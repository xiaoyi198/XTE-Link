use super::xte_link_bridge::{Backend, Failure, Group, Operation, Profile, State};
use crate::{cmd, config::Config, core::handle::Handle};
use async_trait::async_trait;
use serde_json::Value;

pub struct VergeBackend {
    pub ready: fn() -> bool,
}

fn failure(code: &'static str) -> Failure {
    Failure::new(code, false)
}

#[async_trait]
impl Backend for VergeBackend {
    fn ready(&self) -> bool {
        (self.ready)()
    }

    async fn state(&self) -> Result<State, Failure> {
        let profiles = Config::profiles().await.latest_arc();
        let proxies = Handle::mihomo()
            .get_proxies()
            .await
            .map_err(|_| failure("core_unavailable"))?;
        let config = Handle::mihomo()
            .get_base_config()
            .await
            .map_err(|_| failure("core_unavailable"))?;
        let system = cmd::get_sys_proxy()
            .await
            .map_err(|_| failure("system_proxy_unavailable"))?;
        let system =
            serde_json::to_value(system).map_err(|_| failure("system_proxy_unavailable"))?;
        let auto = cmd::get_auto_proxy()
            .await
            .map_err(|_| failure("system_proxy_unavailable"))?;
        let auto = serde_json::to_value(auto).map_err(|_| failure("system_proxy_unavailable"))?;
        let verge = Config::verge().await.latest_arc();
        // The plugin serializes this field as mixedPort, unlike the core's mixed-port.
        // Use its actual Rust fields to avoid mistaking the two wire formats.
        let port = config.mixed_port;
        let expected_proxy = format!(
            "{}:{}",
            verge.proxy_host.as_deref().unwrap_or("127.0.0.1"),
            port
        );
        let proxy_enabled = system
            .get("enable")
            .and_then(Value::as_bool)
            .ok_or_else(|| failure("system_proxy_unavailable"))?;
        let auto_enabled = auto
            .get("enable")
            .and_then(Value::as_bool)
            .ok_or_else(|| failure("system_proxy_unavailable"))?;
        let system_proxy = if verge.proxy_auto_config.unwrap_or(false) {
            let pac_port = crate::utils::server::embedded_server_port()
                .map_err(|_| failure("system_proxy_unavailable"))?;
            let expected = format!("http://127.0.0.1:{pac_port}/commands/pac");
            auto_enabled && auto.get("url").and_then(Value::as_str) == Some(expected.as_str())
        } else {
            proxy_enabled
                && system.get("server").and_then(Value::as_str) == Some(expected_proxy.as_str())
        };
        let groups = proxies
            .proxies
            .iter()
            .filter_map(|(name, item)| {
                let members = item.all.as_ref()?;
                Some(Group {
                    name: name.clone(),
                    kind: item.proxy_type.as_str().to_owned(),
                    current: item.now.clone(),
                    members: members.clone(),
                })
            })
            .collect();
        let list = profiles
            .items
            .iter()
            .flatten()
            .filter_map(|item| {
                let kind = item.itype.as_deref()?;
                if kind != "remote" && kind != "local" {
                    return None;
                }
                let uid = item.uid.as_deref()?;
                Some(Profile {
                    uid: uid.to_owned(),
                    name: item.name.as_deref().unwrap_or(uid).to_owned(),
                    kind: kind.to_owned(),
                    updated: item.updated.map(|v| v as u64),
                })
            })
            .collect();
        Ok(State {
            current_profile: profiles.current.as_deref().map(str::to_owned),
            profiles: list,
            groups,
            system_proxy,
            tun: config.tun.enable,
        })
    }

    async fn execute(&self, operation: Operation) -> Result<(), Failure> {
        // Use the same lock as profile switching/deletion while selecting and recording.
        // Do not retain it for update_profile, which owns its own profile-write locking.
        let _profile_guard = if matches!(&operation, Operation::SelectNode { .. }) {
            Some(crate::config::profiles::PROFILE_WRITE_LOCK.lock().await)
        } else {
            None
        };
        let before = self.state().await?;
        match operation {
            Operation::UpdateProfile { uid } => {
                if !before
                    .profiles
                    .iter()
                    .any(|p| p.uid == uid && p.kind == "remote")
                {
                    return Err(failure("remote_profile_not_found"));
                }
                cmd::update_profile(uid.clone().into(), None)
                    .await
                    .map_err(|_| Failure::new("profile_update_failed_or_unconfirmed", true))?;
                let after = self
                    .state()
                    .await
                    .map_err(|_| Failure::new("readback_failed", true))?;
                if !after
                    .profiles
                    .iter()
                    .any(|p| p.uid == uid && p.kind == "remote")
                {
                    return Err(Failure::new("profile_readback_mismatch", true));
                }
            }
            Operation::SelectNode { group, node } => {
                let selected = before
                    .groups
                    .iter()
                    .find(|g| g.name == group)
                    .ok_or_else(|| failure("group_not_found"))?;
                if selected.kind != "Selector"
                    || !selected.members.contains(&node)
                    || before.current_profile.is_none()
                {
                    return Err(failure("selection_not_supported"));
                }
                Handle::mihomo()
                    .select_node_for_group(&group, &node)
                    .await
                    .map_err(|_| Failure::new("selection_unconfirmed", true))?;
                let current = Config::profiles()
                    .await
                    .latest_arc()
                    .current
                    .as_deref()
                    .map(str::to_owned);
                if current != before.current_profile {
                    return Err(Failure::new("profile_changed", true));
                }
                cmd::record_selected_node(group.clone(), node.clone())
                    .await
                    .map_err(|_| Failure::new("selection_persistence_failed", true))?;
                cmd::sync_tray_proxy_selection()
                    .await
                    .map_err(|_| Failure::new("tray_sync_failed", true))?;
                let after = self
                    .state()
                    .await
                    .map_err(|_| Failure::new("readback_failed", true))?;
                if !after
                    .groups
                    .iter()
                    .any(|g| g.name == group && g.current.as_deref() == Some(node.as_str()))
                {
                    return Err(Failure::new("selection_readback_mismatch", true));
                }
                let profiles = Config::profiles().await.latest_arc();
                if profiles.current.as_deref() != before.current_profile.as_deref()
                    || !profiles.items.iter().flatten().any(|p| {
                        p.uid.as_deref() == before.current_profile.as_deref()
                            && p.selected.iter().flatten().any(|s| {
                                s.name.as_deref() == Some(group.as_str())
                                    && s.now.as_deref() == Some(node.as_str())
                            })
                    })
                {
                    return Err(Failure::new("selection_persistence_mismatch", true));
                }
            }
            Operation::SetSystemProxy { enabled } => {
                if before.system_proxy == enabled
                    && Config::verge()
                        .await
                        .latest_arc()
                        .enable_system_proxy
                        .unwrap_or(false)
                        == enabled
                {
                    return Ok(());
                }
                cmd::patch_verge_config(crate::config::IVerge {
                    enable_system_proxy: Some(enabled),
                    ..Default::default()
                })
                .await
                .map_err(|_| Failure::new("system_proxy_unconfirmed", true))?;
                if self
                    .state()
                    .await
                    .map_err(|_| Failure::new("readback_failed", true))?
                    .system_proxy
                    != enabled
                {
                    return Err(Failure::new("system_proxy_readback_mismatch", true));
                }
            }
            Operation::SetTun { enabled } => {
                if before.tun == enabled
                    && Config::verge()
                        .await
                        .latest_arc()
                        .enable_tun_mode
                        .unwrap_or(false)
                        == enabled
                {
                    return Ok(());
                }
                cmd::patch_verge_config(crate::config::IVerge {
                    enable_tun_mode: Some(enabled),
                    ..Default::default()
                })
                .await
                .map_err(|_| Failure::new("tun_unconfirmed", true))?;
                if self
                    .state()
                    .await
                    .map_err(|_| Failure::new("readback_failed", true))?
                    .tun
                    != enabled
                {
                    return Err(Failure::new("tun_readback_mismatch", true));
                }
            }
        }
        Ok(())
    }
}
