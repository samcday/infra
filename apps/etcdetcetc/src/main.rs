mod cluster;
mod crd;
mod etcd;
mod health;
mod leadership;
mod tenant;

use anyhow::{Context, Result, anyhow};
use clap::{Parser, Subcommand};
use kube::CustomResourceExt;
use std::{
    collections::HashMap,
    error::Error as StdError,
    net::SocketAddr,
    sync::{Arc, RwLock as StdRwLock},
    time::Duration,
};
use tokio::sync::RwLock;
use tokio::{
    signal::unix::{SignalKind, signal},
    sync::watch,
};
use tracing_subscriber::{EnvFilter, fmt::format::FmtSpan};

const HEALTH_ADDRESS: &str = "0.0.0.0:8080";

#[derive(Parser)]
#[command(name = "etcdetcetc")]
struct Cli {
    /// Restrict the controller to these namespaces. Can be repeated.
    /// When empty, the controller operates cluster-wide.
    #[arg(long = "allowed-namespace")]
    allowed_namespaces: Vec<String>,

    /// Address for the kubelet health, readiness, and leadership endpoints.
    #[arg(long, default_value = HEALTH_ADDRESS)]
    health_address: SocketAddr,

    /// Pre-created Lease used to coordinate the active controller replica.
    #[arg(long, default_value = "etcdetcetc-leader")]
    leader_election_lease_name: String,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Print CRD manifests as JSON to stdout
    Crds,
}

fn print_crds() {
    let list = serde_json::json!({
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            crd::EtcdCluster::crd(),
            crd::EtcdTenant::crd(),
        ]
    });
    println!("{}", serde_json::to_string_pretty(&list).unwrap());
}

pub(crate) fn format_error_chain(mut error: &dyn StdError) -> String {
    let mut chain = vec![error.to_string()];
    while let Some(source) = error.source() {
        chain.push(source.to_string());
        error = source;
    }
    chain.join(": ")
}

#[tokio::main]
async fn main() -> Result<()> {
    let Cli {
        command,
        allowed_namespaces,
        health_address,
        leader_election_lease_name,
    } = Cli::parse();

    if let Some(Commands::Crds) = command {
        print_crds();
        return Ok(());
    }

    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .json()
        .with_span_events(FmtSpan::CLOSE)
        .init();

    tracing::info!("etcdetcetc starting");

    let config = kube::Config::infer().await?;
    tracing::info!(
        cluster_url = %config.cluster_url,
        default_namespace = %config.default_namespace,
        kubernetes_service_host = ?std::env::var("KUBERNETES_SERVICE_HOST").ok(),
        kubernetes_service_port = ?std::env::var("KUBERNETES_SERVICE_PORT").ok(),
        "loaded kubernetes client config"
    );

    let election_client = kube::Client::try_from(config.clone())?;
    let client = kube::Client::try_from(config)?;
    tracing::info!("connected to kubernetes");

    let pod_name = required_env("POD_NAME")?;
    let pod_namespace = required_env("POD_NAMESPACE")?;
    let pod_uid = required_env("POD_UID")?;
    let boot_nonce = rand::random::<u128>();
    let identity = format!("{pod_uid}_{boot_nonce:032x}");
    tracing::info!(
        pod_name,
        pod_uid,
        identity,
        "configured leader-election identity"
    );
    let (leadership_gate, leadership_guard) = leadership::channel();

    let cluster_clients: cluster::ClusterClients = Arc::new(RwLock::new(HashMap::new()));
    let cluster_context = cluster::ClusterContext {
        client: client.clone(),
        clients: cluster_clients.clone(),
        allowed_namespaces: allowed_namespaces.clone(),
        config_hashes: Arc::new(StdRwLock::new(HashMap::new())),
        leadership: leadership_guard.clone(),
    };

    let tenant_context = tenant::TenantContext {
        client: client.clone(),
        allowed_namespaces,
        leadership: leadership_guard,
    };

    let election_config = leadership::LeaderElectionConfig::production(
        pod_namespace,
        leader_election_lease_name,
        identity,
    );
    let health_state = Arc::new(health::HealthState::new(
        election_config.elector_stall_timeout,
        election_config.api_stale_timeout,
    ));
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    let mut health_task = tokio::spawn(health::serve(
        health_address,
        health_state.clone(),
        shutdown_rx.clone(),
    ));
    let mut election_task = tokio::spawn(leadership::run(
        election_client,
        election_config,
        health_state.clone(),
        leadership_gate,
        cluster_context,
        tenant_context,
        shutdown_rx,
    ));

    let mut sigint = signal(SignalKind::interrupt())?;
    let mut sigterm = signal(SignalKind::terminate())?;

    enum Exit {
        Election(std::result::Result<Result<()>, tokio::task::JoinError>),
        Health(std::result::Result<Result<()>, tokio::task::JoinError>),
        Signal,
    }

    let exit = tokio::select! {
        result = &mut election_task => Exit::Election(result),
        result = &mut health_task => Exit::Health(result),
        _ = sigint.recv() => {
            tracing::info!("received SIGINT, shutting down");
            Exit::Signal
        }
        _ = sigterm.recv() => {
            tracing::info!("received SIGTERM, shutting down");
            Exit::Signal
        }
    };

    health_state.begin_shutdown();
    let _ = shutdown_tx.send(true);

    match exit {
        Exit::Signal => {
            await_shutdown("leader election", &mut election_task).await?;
            await_shutdown("health server", &mut health_task).await?;
            Ok(())
        }
        Exit::Election(result) => {
            await_shutdown("health server", &mut health_task).await?;
            Err(background_task_ended("leader election", result))
        }
        Exit::Health(result) => {
            await_shutdown("leader election", &mut election_task).await?;
            Err(background_task_ended("health server", result))
        }
    }
}

fn required_env(name: &str) -> Result<String> {
    let value = std::env::var(name).with_context(|| format!("{name} must be set"))?;
    if value.is_empty() {
        return Err(anyhow!("{name} must not be empty"));
    }
    Ok(value)
}

async fn await_shutdown<T>(
    name: &str,
    task: &mut tokio::task::JoinHandle<Result<T>>,
) -> Result<()> {
    match tokio::time::timeout(Duration::from_secs(5), &mut *task).await {
        Ok(result) => result
            .map_err(|error| anyhow!("{name} task failed during shutdown: {error}"))?
            .map(|_| ()),
        Err(_) => {
            task.abort();
            Err(anyhow!("{name} task did not stop within five seconds"))
        }
    }
}

fn background_task_ended<T>(
    name: &str,
    result: std::result::Result<Result<T>, tokio::task::JoinError>,
) -> anyhow::Error {
    match result {
        Ok(Ok(_)) => anyhow!("{name} task exited unexpectedly"),
        Ok(Err(error)) => anyhow!("{name} task failed: {error:#}"),
        Err(err) => anyhow!("{name} task failed: {err}"),
    }
}
