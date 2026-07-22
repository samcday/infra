use std::{future::Future, sync::Arc, time::Duration};

use anyhow::{Context, Result, anyhow};
use chrono::Utc;
use k8s_openapi::{
    api::coordination::v1::{Lease, LeaseSpec},
    apimachinery::pkg::apis::meta::v1::MicroTime,
};
use kube::{Api, Client, ResourceExt, api::PostParams};
use tokio::{sync::watch, task::JoinError, time::Instant};
use tracing::{info, warn};

use crate::{
    cluster::{self, ClusterContext},
    health::HealthState,
    tenant::{self, TenantContext},
};

#[derive(Clone, Debug)]
pub struct LeaderElectionConfig {
    pub namespace: String,
    pub lease_name: String,
    pub identity: String,
    pub lease_duration: Duration,
    pub renew_deadline: Duration,
    pub retry_period: Duration,
    pub api_timeout: Duration,
    pub elector_stall_timeout: Duration,
    pub api_stale_timeout: Duration,
}

#[derive(Clone, Copy, Debug)]
struct LeadershipPermit {
    epoch: u64,
    active: bool,
    hard_deadline: Instant,
}

#[derive(Debug)]
pub struct LeadershipGate {
    sender: watch::Sender<LeadershipPermit>,
    epoch: u64,
}

#[derive(Clone, Debug)]
pub struct LeadershipGuard {
    receiver: watch::Receiver<LeadershipPermit>,
}

pub fn channel() -> (LeadershipGate, LeadershipGuard) {
    let initial = LeadershipPermit {
        epoch: 0,
        active: false,
        hard_deadline: Instant::now(),
    };
    let (sender, receiver) = watch::channel(initial);
    (
        LeadershipGate { sender, epoch: 0 },
        LeadershipGuard { receiver },
    )
}

impl LeadershipGate {
    fn activate(&mut self, hard_deadline: Instant) -> Result<()> {
        self.epoch = self
            .epoch
            .checked_add(1)
            .ok_or_else(|| anyhow!("leader-election epoch overflow"))?;
        self.sender.send_replace(LeadershipPermit {
            epoch: self.epoch,
            active: true,
            hard_deadline,
        });
        Ok(())
    }

    fn renew(&self, hard_deadline: Instant) -> Result<()> {
        let current = *self.sender.borrow();
        if !current.active || current.epoch != self.epoch {
            return Err(anyhow!("cannot renew an inactive leadership permit"));
        }
        self.sender.send_replace(LeadershipPermit {
            hard_deadline,
            ..current
        });
        Ok(())
    }

    fn revoke(&self) {
        let current = *self.sender.borrow();
        self.sender.send_replace(LeadershipPermit {
            active: false,
            hard_deadline: Instant::now(),
            ..current
        });
    }
}

impl LeadershipGuard {
    /// Resolves before the guarded reconcile is polled again once the permit is
    /// explicitly revoked or its monotonic renew deadline passes. Callers use
    /// this as the first branch of a biased select around the entire reconcile.
    pub async fn wait_until_inactive_or_expired(&mut self) {
        let starting_epoch = self.receiver.borrow().epoch;
        loop {
            let permit = *self.receiver.borrow_and_update();
            if !permit.active
                || permit.epoch != starting_epoch
                || Instant::now() >= permit.hard_deadline
            {
                return;
            }
            tokio::select! {
                biased;
                _ = tokio::time::sleep_until(permit.hard_deadline) => return,
                changed = self.receiver.changed() => {
                    if changed.is_err() {
                        return;
                    }
                }
            }
        }
    }
}

impl LeaderElectionConfig {
    pub fn production(namespace: String, lease_name: String, identity: String) -> Self {
        Self {
            namespace,
            lease_name,
            identity,
            lease_duration: Duration::from_secs(30),
            renew_deadline: Duration::from_secs(10),
            retry_period: Duration::from_secs(2),
            api_timeout: Duration::from_secs(2),
            elector_stall_timeout: Duration::from_secs(10),
            api_stale_timeout: Duration::from_secs(10),
        }
    }

    fn validate(&self) -> Result<()> {
        if self.namespace.is_empty() || self.lease_name.is_empty() || self.identity.is_empty() {
            return Err(anyhow!(
                "leader-election namespace, Lease name, and identity must be nonempty"
            ));
        }
        if self.retry_period.is_zero()
            || self.api_timeout.is_zero()
            || self.renew_deadline <= self.retry_period + self.api_timeout.saturating_mul(2)
            || self.lease_duration <= self.renew_deadline
            || self.elector_stall_timeout <= self.api_timeout
            || self.api_stale_timeout < self.renew_deadline
        {
            return Err(anyhow!("unsafe leader-election timing configuration"));
        }
        Ok(())
    }

    fn lease_duration_seconds(&self) -> Result<i32> {
        i32::try_from(self.lease_duration.as_secs())
            .context("leader-election lease duration does not fit in i32 seconds")
    }
}

#[derive(Debug, Default)]
struct LeaseObservation {
    record_version: Option<String>,
    first_observed_at: Option<Instant>,
}

impl LeaseObservation {
    /// Use local monotonic observation time instead of another Pod's wall clock.
    /// A foreign record must remain byte-version-stable for a full lease duration
    /// before this candidate is allowed to attempt a resourceVersion-guarded take.
    fn foreign_record_expired(
        &mut self,
        record_version: &str,
        now: Instant,
        lease_duration: Duration,
    ) -> bool {
        if self.record_version.as_deref() != Some(record_version) {
            self.record_version = Some(record_version.to_string());
            self.first_observed_at = Some(now);
            return false;
        }

        self.first_observed_at
            .is_some_and(|observed| now.saturating_duration_since(observed) >= lease_duration)
    }

    fn reset(&mut self) {
        self.record_version = None;
        self.first_observed_at = None;
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AcquireOutcome {
    Acquired,
    Waiting,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RenewOutcome {
    Renewed,
    Lost,
}

pub async fn run(
    client: Client,
    config: LeaderElectionConfig,
    health: Arc<HealthState>,
    mut leadership_gate: LeadershipGate,
    cluster_context: ClusterContext,
    tenant_context: TenantContext,
    mut shutdown: watch::Receiver<bool>,
) -> Result<()> {
    config.validate()?;
    let leases = Api::<Lease>::namespaced(client, &config.namespace);
    let mut observation = LeaseObservation::default();

    info!(
        namespace = config.namespace,
        lease = config.lease_name,
        identity = config.identity,
        "starting leader election"
    );

    loop {
        if *shutdown.borrow() {
            return Ok(());
        }
        health.elector_heartbeat();

        match try_acquire(&leases, &config, &mut observation).await {
            Ok(AcquireOutcome::Acquired) => {
                health.api_success();
                observation.reset();
                return run_leader_session(
                    &leases,
                    &config,
                    health.clone(),
                    &mut leadership_gate,
                    cluster_context.clone(),
                    tenant_context.clone(),
                    &mut shutdown,
                )
                .await;
            }
            Ok(AcquireOutcome::Waiting) => health.api_success(),
            Err(error) => warn!(
                error = %error,
                error_chain = %crate::format_error_chain(error.as_ref()),
                "leader-election acquisition attempt failed"
            ),
        }

        health.elector_heartbeat();
        tokio::select! {
            _ = tokio::time::sleep(config.retry_period) => {}
            result = shutdown.changed() => {
                if result.is_err() || *shutdown.borrow() {
                    return Ok(());
                }
            }
        }
    }
}

async fn try_acquire(
    leases: &Api<Lease>,
    config: &LeaderElectionConfig,
    observation: &mut LeaseObservation,
) -> Result<AcquireOutcome> {
    let lease = match bounded(config.api_timeout, leases.get(&config.lease_name)).await {
        Ok(lease) => lease,
        Err(BoundedApiError::Kube(error)) if api_code(&error) == Some(404) => {
            return Err(anyhow!(
                "declaratively managed leader-election Lease {}/{} is absent",
                config.namespace,
                config.lease_name
            ));
        }
        Err(error) => return Err(error.into()),
    };
    validate_lease(&lease, config)?;

    let holder = lease
        .spec
        .as_ref()
        .and_then(|spec| spec.holder_identity.as_deref())
        .filter(|holder| !holder.is_empty());
    let held_by_us = holder == Some(config.identity.as_str());
    let lease_uid = lease
        .uid()
        .ok_or_else(|| anyhow!("leader-election Lease has no UID"))?;
    let resource_version = lease
        .resource_version()
        .ok_or_else(|| anyhow!("leader-election Lease has no resourceVersion"))?;
    let record_version = format!("{lease_uid}/{resource_version}");

    if !held_by_us
        && !observation.foreign_record_expired(
            &record_version,
            Instant::now(),
            config.lease_duration,
        )
    {
        return Ok(AcquireOutcome::Waiting);
    }

    let updated = update_lease(lease, config, !held_by_us)?;
    match bounded(
        config.api_timeout,
        leases.replace(&config.lease_name, &PostParams::default(), &updated),
    )
    .await
    {
        Ok(acknowledged) => {
            validate_lease(&acknowledged, config)?;
            if lease_holder(&acknowledged) != Some(config.identity.as_str()) {
                return Err(anyhow!(
                    "leader-election update acknowledgement names another holder"
                ));
            }
            Ok(AcquireOutcome::Acquired)
        }
        Err(BoundedApiError::Kube(error)) if api_code(&error) == Some(409) => {
            Ok(AcquireOutcome::Waiting)
        }
        Err(error) => Err(error.into()),
    }
}

async fn run_leader_session(
    leases: &Api<Lease>,
    config: &LeaderElectionConfig,
    health: Arc<HealthState>,
    leadership_gate: &mut LeadershipGate,
    cluster_context: ClusterContext,
    tenant_context: TenantContext,
    shutdown: &mut watch::Receiver<bool>,
) -> Result<()> {
    info!(identity = config.identity, "leader election acquired");
    let mut hard_deadline = Instant::now() + config.renew_deadline;
    leadership_gate.activate(hard_deadline)?;
    let mut cluster_task = tokio::spawn(cluster::run(cluster_context));
    let mut tenant_task = tokio::spawn(tenant::run(tenant_context));
    health.set_leader(true);

    loop {
        tokio::select! {
            biased;
            _ = tokio::time::sleep_until(hard_deadline) => {
                warn!(identity = config.identity, "leader-election hard renew deadline expired");
                stop_controllers(leadership_gate, &health, &mut cluster_task, &mut tenant_task).await;
                return Err(anyhow!("leadership lost: hard renew deadline expired"));
            }
            result = &mut cluster_task => {
                leadership_gate.revoke();
                health.set_leader(false);
                tenant_task.abort();
                let _ = tenant_task.await;
                return Err(controller_task_ended("EtcdCluster", result));
            }
            result = &mut tenant_task => {
                leadership_gate.revoke();
                health.set_leader(false);
                cluster_task.abort();
                let _ = cluster_task.await;
                return Err(controller_task_ended("EtcdTenant", result));
            }
            result = shutdown.changed() => {
                if result.is_err() || *shutdown.borrow() {
                    stop_controllers(leadership_gate, &health, &mut cluster_task, &mut tenant_task).await;
                    return Ok(());
                }
            }
            _ = tokio::time::sleep(config.retry_period) => {
                health.elector_heartbeat();
                match tokio::time::timeout_at(hard_deadline, try_renew(leases, config)).await {
                    Ok(Ok(RenewOutcome::Renewed)) if Instant::now() < hard_deadline => {
                        hard_deadline = Instant::now() + config.renew_deadline;
                        if let Err(error) = leadership_gate.renew(hard_deadline) {
                            stop_controllers(leadership_gate, &health, &mut cluster_task, &mut tenant_task).await;
                            return Err(error.context("failed to renew the in-process leadership permit"));
                        }
                        health.api_success();
                    }
                    Ok(Ok(RenewOutcome::Renewed)) | Err(_) => {
                        warn!(identity = config.identity, "leader-election renewal reached the hard deadline");
                        stop_controllers(leadership_gate, &health, &mut cluster_task, &mut tenant_task).await;
                        return Err(anyhow!("leadership lost: renewal reached the hard deadline"));
                    }
                    Ok(Ok(RenewOutcome::Lost)) => {
                        health.api_success();
                        warn!(identity = config.identity, "leader-election Lease is held by another identity");
                        stop_controllers(leadership_gate, &health, &mut cluster_task, &mut tenant_task).await;
                        return Err(anyhow!("leadership lost: Lease holder changed"));
                    }
                    Ok(Err(error)) => {
                        warn!(
                            error = %error,
                            error_chain = %crate::format_error_chain(error.as_ref()),
                            "leader-election renewal attempt failed"
                        );
                    }
                }
                health.elector_heartbeat();
            }
        }
    }
}

async fn try_renew(leases: &Api<Lease>, config: &LeaderElectionConfig) -> Result<RenewOutcome> {
    let lease = match bounded(config.api_timeout, leases.get(&config.lease_name)).await {
        Ok(lease) => lease,
        Err(BoundedApiError::Kube(error)) if api_code(&error) == Some(404) => {
            return Ok(RenewOutcome::Lost);
        }
        Err(error) => return Err(error.into()),
    };
    validate_lease(&lease, config)?;
    let holder = lease
        .spec
        .as_ref()
        .and_then(|spec| spec.holder_identity.as_deref());
    if holder != Some(config.identity.as_str()) {
        return Ok(RenewOutcome::Lost);
    }

    let updated = update_lease(lease, config, false)?;
    match bounded(
        config.api_timeout,
        leases.replace(&config.lease_name, &PostParams::default(), &updated),
    )
    .await
    {
        Ok(acknowledged) => {
            validate_lease(&acknowledged, config)?;
            if lease_holder(&acknowledged) != Some(config.identity.as_str()) {
                return Ok(RenewOutcome::Lost);
            }
            Ok(RenewOutcome::Renewed)
        }
        Err(BoundedApiError::Kube(error)) if api_code(&error) == Some(409) => {
            Ok(RenewOutcome::Lost)
        }
        Err(error) => Err(error.into()),
    }
}

fn validate_lease(lease: &Lease, config: &LeaderElectionConfig) -> Result<()> {
    if lease.name_any() != config.lease_name {
        return Err(anyhow!("leader-election Lease has an unexpected name"));
    }
    if lease.namespace().as_deref() != Some(config.namespace.as_str()) {
        return Err(anyhow!("leader-election Lease has an unexpected namespace"));
    }
    if lease.uid().is_none() {
        return Err(anyhow!("leader-election Lease has no UID"));
    }
    let Some(spec) = &lease.spec else {
        return Ok(());
    };
    if spec.strategy.is_some() || spec.preferred_holder.is_some() {
        return Err(anyhow!(
            "leader-election Lease unexpectedly uses coordinated-election fields"
        ));
    }

    match spec.holder_identity.as_deref() {
        None => {
            if spec.acquire_time.is_some()
                || spec.renew_time.is_some()
                || spec.lease_duration_seconds.is_some()
                || spec.lease_transitions.is_some()
            {
                return Err(anyhow!(
                    "vacant leader-election Lease contains partial holder state"
                ));
            }
        }
        Some("") => {
            return Err(anyhow!(
                "leader-election Lease has an empty holder identity"
            ));
        }
        Some(_) => {
            if spec.lease_duration_seconds != Some(config.lease_duration_seconds()?) {
                return Err(anyhow!(
                    "leader-election Lease duration differs from the configured duration"
                ));
            }
            if spec.acquire_time.is_none()
                || spec.renew_time.is_none()
                || spec
                    .lease_transitions
                    .is_none_or(|transitions| transitions < 0)
            {
                return Err(anyhow!("leader-election Lease holder record is incomplete"));
            }
        }
    }
    Ok(())
}

fn lease_holder(lease: &Lease) -> Option<&str> {
    lease
        .spec
        .as_ref()
        .and_then(|spec| spec.holder_identity.as_deref())
        .filter(|holder| !holder.is_empty())
}

fn update_lease(
    mut lease: Lease,
    config: &LeaderElectionConfig,
    acquisition: bool,
) -> Result<Lease> {
    if lease.resource_version().is_none() {
        return Err(anyhow!("leader-election Lease has no resourceVersion"));
    }
    let now = MicroTime(Utc::now());
    let spec = lease.spec.get_or_insert_with(LeaseSpec::default);
    if acquisition {
        spec.acquire_time = Some(now.clone());
        spec.lease_transitions = Some(match spec.holder_identity.as_deref() {
            None => 0,
            Some(_) => spec
                .lease_transitions
                .and_then(|transitions| transitions.checked_add(1))
                .ok_or_else(|| anyhow!("leader-election Lease transition counter overflow"))?,
        });
    }
    let renew_time = if acquisition {
        now
    } else {
        strictly_increasing_time(spec.renew_time.as_ref(), now)?
    };
    spec.holder_identity = Some(config.identity.clone());
    spec.lease_duration_seconds = Some(config.lease_duration_seconds()?);
    spec.renew_time = Some(renew_time);
    spec.preferred_holder = None;
    spec.strategy = None;
    Ok(lease)
}

fn strictly_increasing_time(
    previous: Option<&MicroTime>,
    proposed: MicroTime,
) -> Result<MicroTime> {
    let Some(previous) = previous else {
        return Err(anyhow!(
            "active leader-election Lease has no prior renewTime"
        ));
    };
    if proposed.0 > previous.0 {
        return Ok(proposed);
    }
    let next = previous
        .0
        .checked_add_signed(chrono::Duration::microseconds(1))
        .ok_or_else(|| anyhow!("leader-election renewTime overflow"))?;
    Ok(MicroTime(next))
}

async fn stop_controllers(
    leadership_gate: &LeadershipGate,
    health: &HealthState,
    cluster_task: &mut tokio::task::JoinHandle<()>,
    tenant_task: &mut tokio::task::JoinHandle<()>,
) {
    // No await occurs between publishing leadership loss and cancelling both
    // mutation loops. The remaining lease-duration margin separates this
    // cancellation from any resourceVersion-guarded takeover by a follower.
    leadership_gate.revoke();
    health.set_leader(false);
    cluster_task.abort();
    tenant_task.abort();
    let _ = cluster_task.await;
    let _ = tenant_task.await;
}

fn controller_task_ended(name: &str, result: std::result::Result<(), JoinError>) -> anyhow::Error {
    match result {
        Ok(()) => anyhow!("{name} controller task exited unexpectedly"),
        Err(error) => anyhow!("{name} controller task failed: {error}"),
    }
}

#[derive(Debug, thiserror::Error)]
enum BoundedApiError {
    #[error("Kubernetes Lease API call timed out")]
    Timeout,
    #[error(transparent)]
    Kube(#[from] kube::Error),
}

async fn bounded<T, F>(timeout: Duration, future: F) -> std::result::Result<T, BoundedApiError>
where
    F: Future<Output = std::result::Result<T, kube::Error>>,
{
    tokio::time::timeout(timeout, future)
        .await
        .map_err(|_| BoundedApiError::Timeout)?
        .map_err(BoundedApiError::Kube)
}

fn api_code(error: &kube::Error) -> Option<u16> {
    match error {
        kube::Error::Api(response) => Some(response.code),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kube::api::ObjectMeta;

    fn config() -> LeaderElectionConfig {
        LeaderElectionConfig::production(
            "etcdetcetc".to_string(),
            "etcdetcetc-leader".to_string(),
            "pod_uid".to_string(),
        )
    }

    fn lease(holder: Option<&str>, resource_version: Option<&str>) -> Lease {
        let now = MicroTime(Utc::now());
        Lease {
            metadata: ObjectMeta {
                name: Some("etcdetcetc-leader".to_string()),
                namespace: Some("etcdetcetc".to_string()),
                resource_version: resource_version.map(str::to_string),
                uid: Some("11111111-1111-1111-1111-111111111111".to_string()),
                ..ObjectMeta::default()
            },
            spec: holder.map(|holder| LeaseSpec {
                acquire_time: Some(now.clone()),
                holder_identity: Some(holder.to_string()),
                lease_duration_seconds: Some(30),
                lease_transitions: Some(4),
                renew_time: Some(now),
                ..LeaseSpec::default()
            }),
        }
    }

    #[test]
    fn production_timings_leave_a_takeover_safety_margin() {
        let config = config();
        config.validate().unwrap();
        assert!(config.lease_duration > config.renew_deadline);
        assert!(config.renew_deadline > config.retry_period + config.api_timeout.saturating_mul(2));
    }

    #[test]
    fn foreign_record_must_be_stable_for_a_full_local_lease_duration() {
        let mut observation = LeaseObservation::default();
        let start = Instant::now();
        assert!(!observation.foreign_record_expired("10", start, Duration::from_secs(30)));
        assert!(!observation.foreign_record_expired(
            "10",
            start + Duration::from_secs(29),
            Duration::from_secs(30)
        ));
        assert!(observation.foreign_record_expired(
            "10",
            start + Duration::from_secs(30),
            Duration::from_secs(30)
        ));
    }

    #[test]
    fn any_foreign_resource_version_change_restarts_expiry_observation() {
        let mut observation = LeaseObservation::default();
        let start = Instant::now();
        assert!(!observation.foreign_record_expired("10", start, Duration::from_secs(15)));
        assert!(!observation.foreign_record_expired(
            "11",
            start + Duration::from_secs(20),
            Duration::from_secs(15)
        ));
        assert!(!observation.foreign_record_expired(
            "11",
            start + Duration::from_secs(34),
            Duration::from_secs(15)
        ));
        assert!(observation.foreign_record_expired(
            "11",
            start + Duration::from_secs(35),
            Duration::from_secs(15)
        ));
    }

    #[test]
    fn acquisition_changes_holder_and_increments_transition() {
        let updated = update_lease(lease(Some("other"), Some("12")), &config(), true).unwrap();
        let spec = updated.spec.unwrap();
        assert_eq!(spec.holder_identity.as_deref(), Some("pod_uid"));
        assert_eq!(spec.lease_duration_seconds, Some(30));
        assert_eq!(spec.lease_transitions, Some(5));
        assert!(spec.acquire_time.is_some());
        assert!(spec.renew_time.is_some());
    }

    #[test]
    fn renewal_preserves_acquisition_and_transition() {
        let mut existing = lease(Some("pod_uid"), Some("12"));
        let acquired = MicroTime(Utc::now() - chrono::Duration::seconds(5));
        existing.spec.as_mut().unwrap().acquire_time = Some(acquired.clone());
        let updated = update_lease(existing, &config(), false).unwrap();
        let spec = updated.spec.unwrap();
        assert_eq!(spec.acquire_time, Some(acquired));
        assert_eq!(spec.lease_transitions, Some(4));
        assert!(spec.renew_time.is_some());
    }

    #[test]
    fn coordinated_election_fields_are_rejected_fail_closed() {
        let mut unexpected = lease(Some("other"), Some("12"));
        unexpected.spec.as_mut().unwrap().strategy = Some("OldestEmulationVersion".to_string());
        assert!(validate_lease(&unexpected, &config()).is_err());
    }

    #[test]
    fn mismatched_lease_duration_is_rejected_fail_closed() {
        let mut unexpected = lease(Some("other"), Some("12"));
        unexpected.spec.as_mut().unwrap().lease_duration_seconds = Some(60);
        assert!(validate_lease(&unexpected, &config()).is_err());
    }

    #[test]
    fn declarative_vacant_lease_is_valid_but_partial_state_is_not() {
        let mut vacant = lease(None, Some("1"));
        assert!(validate_lease(&vacant, &config()).is_ok());
        vacant.spec = Some(LeaseSpec {
            lease_transitions: Some(0),
            ..LeaseSpec::default()
        });
        assert!(validate_lease(&vacant, &config()).is_err());
    }

    #[tokio::test]
    async fn permit_revocation_and_epoch_change_cancel_reconcilers() {
        let (mut gate, mut guard) = channel();
        assert!(
            tokio::time::timeout(
                Duration::from_millis(10),
                guard.wait_until_inactive_or_expired()
            )
            .await
            .is_ok()
        );

        gate.activate(Instant::now() + Duration::from_secs(60))
            .unwrap();
        let mut old_epoch_guard = guard.clone();
        let waiter = tokio::spawn(async move {
            old_epoch_guard.wait_until_inactive_or_expired().await;
        });
        tokio::task::yield_now().await;
        gate.revoke();
        gate.activate(Instant::now() + Duration::from_secs(60))
            .unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(10), waiter)
                .await
                .is_ok()
        );
    }

    #[tokio::test(start_paused = true)]
    async fn hard_deadline_cancels_reconcile_without_supervisor_progress() {
        let (mut gate, mut guard) = channel();
        gate.activate(Instant::now() + Duration::from_secs(10))
            .unwrap();
        let waiter = tokio::spawn(async move {
            guard.wait_until_inactive_or_expired().await;
        });
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());
        tokio::time::advance(Duration::from_secs(11)).await;
        waiter.await.unwrap();
    }

    #[test]
    fn transition_overflow_and_non_monotonic_wall_clock_fail_safely() {
        let mut overflow = lease(Some("other"), Some("12"));
        overflow.spec.as_mut().unwrap().lease_transitions = Some(i32::MAX);
        assert!(update_lease(overflow, &config(), true).is_err());

        let previous = MicroTime(Utc::now() + chrono::Duration::seconds(5));
        let proposed = MicroTime(Utc::now());
        let next = strictly_increasing_time(Some(&previous), proposed).unwrap();
        assert!(next.0 > previous.0);
    }
}
