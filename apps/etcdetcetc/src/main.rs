mod crd;

use anyhow::Result;
use tracing_subscriber::{EnvFilter, fmt::format::FmtSpan};

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .json()
        .with_span_events(FmtSpan::CLOSE)
        .init();

    tracing::info!("etcdetcetc starting");

    let client = kube::Client::try_default().await?;
    tracing::info!("connected to kubernetes");

    // TODO: start EtcdCluster and EtcdTenant controllers

    // Park until shutdown signal.
    tokio::signal::ctrl_c().await?;
    Ok(())
}
