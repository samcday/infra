mod cluster;
mod crd;
mod etcd;
mod tenant;

use anyhow::{Result, anyhow};
use clap::{Parser, Subcommand};
use kube::CustomResourceExt;
use std::{collections::HashMap, sync::{Arc, RwLock as StdRwLock}};
use tokio::sync::RwLock;
use tokio::{signal::unix::{SignalKind, signal}, task::JoinError};
use tracing_subscriber::{EnvFilter, fmt::format::FmtSpan};

#[derive(Parser)]
#[command(name = "etcdetcetc")]
struct Cli {
    /// Restrict the controller to these namespaces. Can be repeated.
    /// When empty, the controller operates cluster-wide.
    #[arg(long = "allowed-namespace")]
    allowed_namespaces: Vec<String>,

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

#[tokio::main]
async fn main() -> Result<()> {
    let Cli {
        command,
        allowed_namespaces,
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

    let client = kube::Client::try_default().await?;
    tracing::info!("connected to kubernetes");

    let cluster_clients: cluster::ClusterClients = Arc::new(RwLock::new(HashMap::new()));
    let secret_refs: cluster::SecretRefIndex = Arc::new(StdRwLock::new(HashMap::new()));
    let cluster_context = cluster::ClusterContext {
        client: client.clone(),
        clients: cluster_clients.clone(),
        secret_refs,
        allowed_namespaces: allowed_namespaces.clone(),
        config_hashes: Arc::new(StdRwLock::new(HashMap::new())),
    };

    let tenant_context = tenant::TenantContext {
        client: client.clone(),
        clients: cluster_clients,
        allowed_namespaces,
    };

    let mut cluster_task = tokio::spawn(async move {
        cluster::run(cluster_context).await;
    });

    let mut tenant_task = tokio::spawn(async move {
        tenant::run(tenant_context).await;
    });

    let mut sigint = signal(SignalKind::interrupt())?;
    let mut sigterm = signal(SignalKind::terminate())?;

    tokio::select! {
        result = &mut cluster_task => {
            return Err(controller_task_ended("EtcdCluster", result));
        }
        result = &mut tenant_task => {
            return Err(controller_task_ended("EtcdTenant", result));
        }
        _ = sigint.recv() => {
            tracing::info!("received SIGINT, shutting down");
        }
        _ = sigterm.recv() => {
            tracing::info!("received SIGTERM, shutting down");
        }
    }

    cluster_task.abort();
    tenant_task.abort();
    let _ = cluster_task.await;
    let _ = tenant_task.await;

    Ok(())
}

fn controller_task_ended(name: &str, result: std::result::Result<(), JoinError>) -> anyhow::Error {
    match result {
        Ok(()) => anyhow!("{name} controller task exited unexpectedly"),
        Err(err) => anyhow!("{name} controller task failed: {err}"),
    }
}
