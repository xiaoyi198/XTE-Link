#[path = "../../bridge.rs"]
mod bridge;

use async_trait::async_trait;
use bridge::{Backend, Failure, Group, Operation, Profile, State};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use warp::Filter;

const TOKEN: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct MemoryBackend {
    ready: AtomicBool,
    writes: AtomicUsize,
    state: Mutex<State>,
}

impl MemoryBackend {
    fn new() -> Self {
        Self {
            ready: AtomicBool::new(true),
            writes: AtomicUsize::new(0),
            state: Mutex::new(State {
                current_profile: Some("r1".into()),
                profiles: vec![Profile {
                    uid: "r1".into(),
                    name: "订阅 A".into(),
                    kind: "remote".into(),
                    updated: Some(0),
                }],
                groups: vec![Group {
                    name: "代理组".into(),
                    kind: "Selector".into(),
                    current: Some("节点 A".into()),
                    members: vec!["节点 A".into(), "节点 B".into()],
                }],
                system_proxy: true,
                tun: true,
            }),
        }
    }
}

#[async_trait]
impl Backend for MemoryBackend {
    fn ready(&self) -> bool {
        self.ready.load(Ordering::SeqCst)
    }
    async fn state(&self) -> Result<State, Failure> {
        Ok(self.state.lock().unwrap().clone())
    }
    async fn execute(&self, operation: Operation) -> Result<(), Failure> {
        self.writes.fetch_add(1, Ordering::SeqCst);
        if matches!(&operation, Operation::UpdateProfile { uid } if uid == "panic") {
            panic!("in-memory backend interruption fixture");
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
        let mut state = self.state.lock().unwrap();
        match operation {
            Operation::UpdateProfile { uid } if uid == "uncertain" => {
                return Err(Failure::new("readback_failed", true));
            }
            Operation::UpdateProfile { uid } => {
                let profile = state
                    .profiles
                    .iter_mut()
                    .find(|p| p.uid == uid)
                    .ok_or_else(|| Failure::new("remote_profile_not_found", false))?;
                profile.updated = Some(profile.updated.unwrap_or_default() + 1);
            }
            Operation::SelectNode { group, node } => {
                let group = state
                    .groups
                    .iter_mut()
                    .find(|g| g.name == group)
                    .ok_or_else(|| Failure::new("group_not_found", false))?;
                if !group.members.contains(&node) {
                    return Err(Failure::new("node_not_found", false));
                }
                group.current = Some(node);
            }
            Operation::SetSystemProxy { enabled } => state.system_proxy = enabled,
            Operation::SetTun { enabled } => state.tun = enabled,
        }
        Ok(())
    }
}

#[tokio::main]
async fn main() {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 || args[1] != "--simulate" {
        eprintln!(
            "Only --simulate <instance-record.json> is supported; no real Clash backend is linked."
        );
        std::process::exit(2);
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let filters = warp::header::exact("x-instance-token", TOKEN)
        .and(bridge::routes(Arc::new(MemoryBackend::new())));
    std::fs::write(
        &args[2],
        serde_json::json!({"port":port,"token":TOKEN}).to_string(),
    )
    .unwrap();
    println!("Simulation only: loopback port {port}; all writes affect memory fixtures.");
    warp::serve(filters).incoming(listener).run().await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn command(id: &str, operation: Value) -> Value {
        json!({"v":1,"id":id,"operation":operation})
    }

    #[tokio::test]
    async fn four_actions_and_readback_use_the_shared_routes() {
        let backend = Arc::new(MemoryBackend::new());
        let api = bridge::routes(backend.clone());
        for (index, operation) in [
            json!({"action":"update_profile","uid":"r1"}),
            json!({"action":"select_node","group":"代理组","node":"节点 B"}),
            json!({"action":"set_system_proxy","enabled":false}),
            json!({"action":"set_tun","enabled":false}),
        ]
        .into_iter()
        .enumerate()
        {
            let id = format!("action-{index}");
            let post = warp::test::request()
                .method("POST")
                .path("/xte-link/v1/command")
                .json(&command(&id, operation))
                .reply(&api)
                .await;
            assert_eq!(post.status(), 202);
            for _ in 0..100 {
                let receipt = warp::test::request()
                    .path(&format!("/xte-link/v1/requests/{id}"))
                    .reply(&api)
                    .await;
                let receipt: Value = serde_json::from_slice(receipt.body()).unwrap();
                if receipt["status"] == "done" {
                    break;
                }
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            }
            let receipt = warp::test::request()
                .path(&format!("/xte-link/v1/requests/{id}"))
                .reply(&api)
                .await;
            assert_eq!(
                serde_json::from_slice::<Value>(receipt.body()).unwrap()["status"],
                "done"
            );
        }
        let state = backend.state().await.unwrap();
        assert_eq!(state.profiles[0].updated, Some(1));
        assert_eq!(state.groups[0].current.as_deref(), Some("节点 B"));
        assert!(!state.system_proxy && !state.tun);
        assert_eq!(backend.writes.load(Ordering::SeqCst), 4);
    }

    #[tokio::test]
    async fn duplicate_ids_and_busy_requests_do_not_repeat_actions() {
        let backend = Arc::new(MemoryBackend::new());
        let api = bridge::routes(backend.clone());
        let payload = command("same", json!({"action":"update_profile","uid":"r1"}));
        let first = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&payload)
            .reply(&api)
            .await;
        assert_eq!(first.status(), 202);
        let duplicate = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&payload)
            .reply(&api)
            .await;
        assert_eq!(duplicate.status(), 200);
        for (id, op) in [
            ("other", json!({"action":"update_profile","uid":"r1"})),
            ("same", json!({"action":"set_tun","enabled":false})),
        ] {
            let rejected = warp::test::request()
                .method("POST")
                .path("/xte-link/v1/command")
                .json(&command(id, op))
                .reply(&api)
                .await;
            assert_eq!(rejected.status(), 409);
        }
        tokio::time::sleep(std::time::Duration::from_millis(60)).await;
        let again = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&payload)
            .reply(&api)
            .await;
        assert_eq!(
            serde_json::from_slice::<Value>(again.body()).unwrap()["status"],
            "done"
        );
        assert_eq!(backend.writes.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn schema_methods_auth_and_not_ready_block_writes() {
        let backend = Arc::new(MemoryBackend::new());
        let api = bridge::routes(backend.clone());
        for payload in [
            command("bad", json!({"action":"set_tun","enabled":"false"})),
            command(
                "bad",
                json!({"action":"set_tun","enabled":false,"path":"anything"}),
            ),
            command("bad", json!({"action":"restart"})),
            command("bad/name", json!({"action":"set_tun","enabled":false})),
            json!({"v":2,"id":"bad","operation":{"action":"set_tun","enabled":false}}),
        ] {
            let rejected = warp::test::request()
                .method("POST")
                .path("/xte-link/v1/command")
                .json(&payload)
                .reply(&api)
                .await;
            assert_eq!(rejected.status(), 400);
        }
        let oversized = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .header("content-type", "application/json")
            .body("x".repeat(4097))
            .reply(&api)
            .await;
        assert_eq!(oversized.status(), 413);
        let secured = warp::header::exact("x-instance-token", TOKEN).and(api.clone());
        let denied = warp::test::request()
            .path("/xte-link/v1/state")
            .reply(&secured)
            .await;
        assert!(denied.status().is_client_error());
        let wrong_method = warp::test::request()
            .method("PATCH")
            .path("/xte-link/v1/command")
            .reply(&api)
            .await;
        assert!(wrong_method.status().is_client_error());
        backend.ready.store(false, Ordering::SeqCst);
        let unavailable = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&command(
                "ready",
                json!({"action":"set_tun","enabled":false}),
            ))
            .reply(&api)
            .await;
        assert_eq!(unavailable.status(), 503);
        assert_eq!(backend.writes.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn uncertainty_is_preserved_and_unknown_ids_are_not_reexecuted() {
        let backend = Arc::new(MemoryBackend::new());
        let api = bridge::routes(backend.clone());
        let payload = command(
            "uncertain",
            json!({"action":"update_profile","uid":"uncertain"}),
        );
        warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&payload)
            .reply(&api)
            .await;
        tokio::time::sleep(std::time::Duration::from_millis(60)).await;
        let result = warp::test::request()
            .path("/xte-link/v1/requests/uncertain")
            .reply(&api)
            .await;
        let result: Value = serde_json::from_slice(result.body()).unwrap();
        assert_eq!(result["status"], "failed");
        assert_eq!(result["error"]["uncertain"], true);
        let unknown = warp::test::request()
            .path("/xte-link/v1/requests/missing")
            .reply(&api)
            .await;
        assert_eq!(unknown.status(), 404);
        assert_eq!(backend.writes.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn backend_panic_finishes_receipt_and_releases_busy_slot() {
        let backend = Arc::new(MemoryBackend::new());
        let api = bridge::routes(backend.clone());
        let post = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&command(
                "panic",
                json!({"action":"update_profile","uid":"panic"}),
            ))
            .reply(&api)
            .await;
        assert_eq!(post.status(), 202);
        tokio::time::sleep(std::time::Duration::from_millis(60)).await;
        let receipt = warp::test::request()
            .path("/xte-link/v1/requests/panic")
            .reply(&api)
            .await;
        let receipt: Value = serde_json::from_slice(receipt.body()).unwrap();
        assert_eq!(receipt["status"], "failed");
        assert_eq!(receipt["error"]["code"], "backend_interrupted");
        assert_eq!(receipt["error"]["uncertain"], true);
        let next = warp::test::request()
            .method("POST")
            .path("/xte-link/v1/command")
            .json(&command(
                "after-panic",
                json!({"action":"set_system_proxy","enabled":true}),
            ))
            .reply(&api)
            .await;
        assert_eq!(next.status(), 202);
    }
}
