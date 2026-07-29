use anyhow::{Context, Result, bail};
use k8s_openapi::api::core::v1::Secret;
use kube::api::Api;
use kube::config::{
    AuthInfo, Cluster, ExecConfig, ExecInteractiveMode, KubeConfigOptions, Kubeconfig,
    NamedAuthInfo, NamedCluster, NamedContext,
};
use kube::{Client, Config};
use std::path::{Path, PathBuf};

pub enum ParentAuth {
    CertificateFiles {
        client_cert: PathBuf,
        client_key: PathBuf,
    },
    Exec {
        command: PathBuf,
    },
}

pub async fn fetch_ca_secret(
    server_url: &str,
    server_tls_name: Option<&str>,
    server_ca_path: &Path,
    parent_auth: ParentAuth,
    namespace: &str,
) -> Result<(Vec<u8>, Vec<u8>)> {
    let auth_info = match parent_auth {
        ParentAuth::CertificateFiles {
            client_cert,
            client_key,
        } => AuthInfo {
            client_certificate: Some(client_cert.to_string_lossy().to_string()),
            client_key: Some(client_key.to_string_lossy().to_string()),
            ..Default::default()
        },
        ParentAuth::Exec { command } => AuthInfo {
            exec: Some(ExecConfig {
                api_version: Some("client.authentication.k8s.io/v1".to_string()),
                command: Some(command.to_string_lossy().to_string()),
                args: None,
                env: None,
                drop_env: None,
                interactive_mode: Some(ExecInteractiveMode::Never),
                provide_cluster_info: false,
                cluster: None,
            }),
            ..Default::default()
        },
    };

    let kubeconfig = Kubeconfig {
        clusters: vec![NamedCluster {
            name: "parent".to_string(),
            cluster: Some(Cluster {
                server: Some(server_url.to_string()),
                tls_server_name: server_tls_name.map(str::to_string),
                certificate_authority: Some(server_ca_path.to_string_lossy().to_string()),
                ..Default::default()
            }),
        }],
        auth_infos: vec![NamedAuthInfo {
            name: "parent-admin".to_string(),
            auth_info: Some(auth_info),
        }],
        contexts: vec![NamedContext {
            name: "parent".to_string(),
            context: Some(kube::config::Context {
                cluster: "parent".to_string(),
                user: Some("parent-admin".to_string()),
                namespace: Some(namespace.to_string()),
                ..Default::default()
            }),
        }],
        current_context: Some("parent".to_string()),
        ..Default::default()
    };

    let config = Config::from_custom_kubeconfig(
        kubeconfig,
        &KubeConfigOptions::default(),
    )
    .await
    .context("failed to build parent kube config")?;

    let client = Client::try_from(config)
        .context("failed to create kube client")?;

    let secrets: Api<Secret> = Api::namespaced(client, namespace);
    let secret = secrets
        .get("ca")
        .await
        .with_context(|| format!("failed to get secret 'ca' in namespace '{namespace}'"))?;

    let data = secret.data.context("secret 'ca' has no data")?;

    let tls_key = data
        .get("tls.key")
        .context("secret 'ca' missing tls.key")?
        .0
        .clone();
    let tls_crt = data
        .get("tls.crt")
        .context("secret 'ca' missing tls.crt")?
        .0
        .clone();

    if tls_key.is_empty() || tls_crt.is_empty() {
        bail!("secret 'ca' has empty tls.key or tls.crt");
    }

    Ok((tls_key, tls_crt))
}
