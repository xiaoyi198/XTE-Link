use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
};
use warp::{Filter, http::StatusCode, reply::Response};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Profile {
    pub uid: String,
    pub name: String,
    pub kind: String,
    pub updated: Option<u64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Group {
    pub name: String,
    pub kind: String,
    pub current: Option<String>,
    pub members: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct State {
    pub current_profile: Option<String>,
    pub profiles: Vec<Profile>,
    pub groups: Vec<Group>,
    pub system_proxy: bool,
    pub tun: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    UpdateProfile { uid: String },
    SelectNode { group: String, node: String },
    SetSystemProxy { enabled: bool },
    SetTun { enabled: bool },
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Command {
    pub v: u8,
    pub id: String,
    pub operation: Operation,
}

impl Command {
    fn valid(&self) -> bool {
        let name = |s: &str| !s.is_empty() && s.len() <= 512 && !s.chars().any(char::is_control);
        self.v == 1
            && valid_id(&self.id)
            && match &self.operation {
                Operation::UpdateProfile { uid } => name(uid),
                Operation::SelectNode { group, node } => name(group) && name(node),
                _ => true,
            }
    }
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Failure {
    pub code: &'static str,
    pub uncertain: bool,
}

impl Failure {
    pub fn new(code: &'static str, uncertain: bool) -> Self {
        Self { code, uncertain }
    }
}

#[async_trait]
pub trait Backend: Send + Sync + 'static {
    fn ready(&self) -> bool;
    async fn state(&self) -> Result<State, Failure>;
    async fn execute(&self, operation: Operation) -> Result<(), Failure>;
}

#[derive(Clone, Serialize)]
struct Receipt {
    v: u8,
    id: String,
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<Failure>,
}

struct Entry {
    command: Command,
    receipt: Receipt,
}

fn response<T: Serialize>(value: &T, status: StatusCode) -> Response {
    warp::reply::with_status(warp::reply::json(value), status).into_response()
}

fn error(code: &'static str, status: StatusCode) -> Response {
    response(
        &serde_json::json!({"v":1,"error":{"code":code,"uncertain":false}}),
        status,
    )
}

// The caller wraps all routes with Verge's existing instance-token authentication.
pub fn routes(
    backend: Arc<dyn Backend>,
) -> impl Filter<Extract = (Response,), Error = warp::Rejection> + Clone {
    let entries: Arc<Mutex<VecDeque<Entry>>> = Arc::new(Mutex::new(VecDeque::new()));
    let caps_backend = backend.clone();
    let capabilities = warp::path!("xte-link" / "v1" / "capabilities")
        .and(warp::get())
        .map(move || {
            response(
                &serde_json::json!({"v":1,"target_version":"2.5.8","ready":caps_backend.ready(),
            "operations":["update_profile","select_node","set_system_proxy","set_tun"]}),
                StatusCode::OK,
            )
        });
    let read_backend = backend.clone();
    let state = warp::path!("xte-link" / "v1" / "state")
        .and(warp::get())
        .then(move || {
            let backend = read_backend.clone();
            async move {
                if !backend.ready() {
                    return error("not_ready", StatusCode::SERVICE_UNAVAILABLE);
                }
                match backend.state().await {
                    Ok(state) => {
                        response(&serde_json::json!({"v":1,"state":state}), StatusCode::OK)
                    }
                    Err(failure) => response(
                        &serde_json::json!({"v":1,"error":failure}),
                        StatusCode::SERVICE_UNAVAILABLE,
                    ),
                }
            }
        });
    let read_entries = entries.clone();
    let receipt = warp::path!("xte-link" / "v1" / "requests" / String)
        .and(warp::get())
        .map(move |id: String| {
            if !valid_id(&id) {
                return error("invalid_id", StatusCode::BAD_REQUEST);
            }
            let Ok(entries) = read_entries.lock() else {
                return error("unavailable", StatusCode::SERVICE_UNAVAILABLE);
            };
            match entries.iter().find(|e| e.command.id == id) {
                Some(entry) => response(&entry.receipt, StatusCode::OK),
                None => error("unknown_request", StatusCode::NOT_FOUND),
            }
        });
    let command = warp::path!("xte-link" / "v1" / "command")
        .and(warp::post())
        .and(warp::body::content_length_limit(4096))
        .and(warp::body::json())
        .map(move |command: Command| {
            if !command.valid() {
                return error("invalid_command", StatusCode::BAD_REQUEST);
            }
            if !backend.ready() {
                return error("not_ready", StatusCode::SERVICE_UNAVAILABLE);
            }
            let pending = {
                let Ok(mut rows) = entries.lock() else {
                    return error("unavailable", StatusCode::SERVICE_UNAVAILABLE);
                };
                if let Some(entry) = rows.iter().find(|e| e.command.id == command.id) {
                    return if entry.command == command {
                        response(&entry.receipt, StatusCode::OK)
                    } else {
                        error("request_id_conflict", StatusCode::CONFLICT)
                    };
                }
                if rows.iter().any(|e| e.receipt.status == "pending") {
                    return error("busy", StatusCode::CONFLICT);
                }
                if rows.len() == 32 {
                    rows.pop_front();
                }
                let receipt = Receipt {
                    v: 1,
                    id: command.id.clone(),
                    status: "pending",
                    error: None,
                };
                rows.push_back(Entry {
                    command: command.clone(),
                    receipt: receipt.clone(),
                });
                receipt
            };
            let backend = backend.clone();
            let entries = entries.clone();
            // Completion survives a caller disconnect; only explicit receipt polling follows a timeout.
            tokio::spawn(async move {
                let result = tokio::spawn(async move { backend.execute(command.operation).await })
                    .await
                    .unwrap_or_else(|_| Err(Failure::new("backend_interrupted", true)));
                if let Ok(mut rows) = entries.lock()
                    && let Some(entry) = rows.iter_mut().find(|e| e.command.id == command.id)
                {
                    entry.receipt.status = if result.is_ok() { "done" } else { "failed" };
                    entry.receipt.error = result.err();
                }
            });
            response(&pending, StatusCode::ACCEPTED)
        });
    capabilities
        .or(state)
        .unify()
        .or(receipt)
        .unify()
        .or(command)
        .unify()
}

use warp::Reply;
