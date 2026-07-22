use std::{
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::Result;
use axum::{Router, extract::State, http::StatusCode, routing::get};
use tokio::{net::TcpListener, sync::watch};

pub struct HealthState {
    elector_heartbeat: Mutex<Instant>,
    last_api_success: Mutex<Option<Instant>>,
    elector_stall_timeout: Duration,
    api_stale_timeout: Duration,
    leader: AtomicBool,
    shutting_down: AtomicBool,
}

impl HealthState {
    pub fn new(elector_stall_timeout: Duration, api_stale_timeout: Duration) -> Self {
        Self {
            elector_heartbeat: Mutex::new(Instant::now()),
            last_api_success: Mutex::new(None),
            elector_stall_timeout,
            api_stale_timeout,
            leader: AtomicBool::new(false),
            shutting_down: AtomicBool::new(false),
        }
    }

    pub fn elector_heartbeat(&self) {
        *self
            .elector_heartbeat
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Instant::now();
    }

    pub fn api_success(&self) {
        *self
            .last_api_success
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(Instant::now());
    }

    pub fn set_leader(&self, leader: bool) {
        self.leader.store(leader, Ordering::SeqCst);
    }

    pub fn begin_shutdown(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
        self.leader.store(false, Ordering::SeqCst);
    }

    fn statuses_at(&self, now: Instant) -> HealthStatuses {
        if self.shutting_down.load(Ordering::SeqCst) {
            return HealthStatuses::default();
        }

        let heartbeat = *self
            .elector_heartbeat
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let live = now.saturating_duration_since(heartbeat) <= self.elector_stall_timeout;
        let api_ready = self
            .last_api_success
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .is_some_and(|success| {
                now.saturating_duration_since(success) <= self.api_stale_timeout
            });
        let ready = live && api_ready;

        HealthStatuses {
            live,
            ready,
            leader: ready && self.leader.load(Ordering::SeqCst),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct HealthStatuses {
    live: bool,
    ready: bool,
    leader: bool,
}

pub async fn serve(
    address: SocketAddr,
    state: Arc<HealthState>,
    shutdown: watch::Receiver<bool>,
) -> Result<()> {
    let app = Router::new()
        .route("/healthz", get(liveness))
        .route("/readyz", get(readiness))
        .route("/leaderz", get(leadership))
        .with_state(state);
    let listener = TcpListener::bind(address).await?;
    tracing::info!(%address, "health server listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(wait_for_shutdown(shutdown))
        .await?;
    Ok(())
}

async fn wait_for_shutdown(mut shutdown: watch::Receiver<bool>) {
    while !*shutdown.borrow() {
        if shutdown.changed().await.is_err() {
            return;
        }
    }
}

async fn liveness(State(state): State<Arc<HealthState>>) -> StatusCode {
    status(state.statuses_at(Instant::now()).live)
}

async fn readiness(State(state): State<Arc<HealthState>>) -> StatusCode {
    status(state.statuses_at(Instant::now()).ready)
}

async fn leadership(State(state): State<Arc<HealthState>>) -> StatusCode {
    status(state.statuses_at(Instant::now()).leader)
}

fn status(ok: bool) -> StatusCode {
    if ok {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_requires_recent_api_success_but_not_leadership() {
        let state = HealthState::new(Duration::from_secs(10), Duration::from_secs(10));
        let now = Instant::now();
        let initial = state.statuses_at(now);
        assert!(initial.live);
        assert!(!initial.ready);
        assert!(!initial.leader);

        state.api_success();
        let follower = state.statuses_at(Instant::now());
        assert!(follower.live);
        assert!(follower.ready);
        assert!(!follower.leader);

        state.set_leader(true);
        assert!(state.statuses_at(Instant::now()).leader);
    }

    #[test]
    fn stale_elector_or_api_is_fail_closed() {
        let state = HealthState::new(Duration::from_secs(10), Duration::from_secs(10));
        let now = Instant::now();
        state.elector_heartbeat();
        state.api_success();
        state.set_leader(true);

        let stale = state.statuses_at(now + Duration::from_secs(11));
        assert!(!stale.live);
        assert!(!stale.ready);
        assert!(!stale.leader);
    }

    #[test]
    fn shutdown_immediately_fails_every_endpoint() {
        let state = HealthState::new(Duration::from_secs(10), Duration::from_secs(10));
        state.api_success();
        state.set_leader(true);
        state.begin_shutdown();
        assert_eq!(state.statuses_at(Instant::now()), HealthStatuses::default());
    }
}
