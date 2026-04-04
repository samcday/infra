use std::fs;
use std::io::Write;
use std::process::{Command, Stdio};

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};

const CLUSTER_NAME: &str = "etcdetcetc";
const DEV_DIR: &str = ".dev";

#[derive(Parser)]
#[command(name = "xtask")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Create a kind cluster with etcd access and emit sourceable env vars
    DevUp,
    /// Tear down the kind cluster
    DevDown,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::DevUp => cmd_dev_up(),
        Commands::DevDown => cmd_dev_down(),
    }
}

fn cmd_dev_up() -> Result<()> {
    eprintln!("Starting dev-up...");

    ensure_tool("kind")?;
    ensure_tool("docker")?;
    ensure_tool("kubectl")?;

    let clusters =
        run_stdout("kind", &["get", "clusters"]).context("checking existing kind clusters")?;
    let cluster_exists = clusters.lines().any(|line| line.trim() == CLUSTER_NAME);
    if cluster_exists {
        eprintln!("Kind cluster '{CLUSTER_NAME}' already exists, reusing it");
    } else {
        run("kind", &["create", "cluster", "--name", CLUSTER_NAME])
            .context("creating kind cluster")?;
    }

    let cwd = std::env::current_dir().context("getting current directory")?;
    let dev_dir = cwd.join(DEV_DIR);
    fs::create_dir_all(&dev_dir)
        .with_context(|| format!("creating {} directory", dev_dir.display()))?;

    let kubeconfig = dev_dir.join("kubeconfig");
    let kubeconfig_str = kubeconfig.to_string_lossy().into_owned();
    run(
        "kind",
        &[
            "export",
            "kubeconfig",
            "--name",
            CLUSTER_NAME,
            "--kubeconfig",
            &kubeconfig_str,
        ],
    )
    .with_context(|| format!("exporting kind kubeconfig to {}", kubeconfig.display()))?;

    // SAFETY: xtask is a single-threaded synchronous CLI. Setting this process env var
    // before spawning child commands is safe and ensures kubectl/cargo see KUBECONFIG.
    unsafe {
        std::env::set_var("KUBECONFIG", &kubeconfig_str);
    }

    let container_name = format!("{CLUSTER_NAME}-control-plane");
    let ca_crt = dev_dir.join("ca.crt");
    let tls_crt = dev_dir.join("tls.crt");
    let tls_key = dev_dir.join("tls.key");

    let cp_ca_src = format!("{container_name}:/etc/kubernetes/pki/etcd/ca.crt");
    let cp_ca_dst = ca_crt.to_string_lossy().into_owned();
    run("docker", &["cp", &cp_ca_src, &cp_ca_dst])
        .context("copying etcd CA cert from container")?;

    let cp_crt_src = format!("{container_name}:/etc/kubernetes/pki/apiserver-etcd-client.crt");
    let cp_crt_dst = tls_crt.to_string_lossy().into_owned();
    run("docker", &["cp", &cp_crt_src, &cp_crt_dst])
        .context("copying apiserver etcd client cert from container")?;

    let cp_key_src = format!("{container_name}:/etc/kubernetes/pki/apiserver-etcd-client.key");
    let cp_key_dst = tls_key.to_string_lossy().into_owned();
    run("docker", &["cp", &cp_key_src, &cp_key_dst])
        .context("copying apiserver etcd client key from container")?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tls_key, std::fs::Permissions::from_mode(0o600))
            .context("setting permissions on tls.key")?;
    }

    let inspect_ip = run_stdout(
        "docker",
        &[
            "inspect",
            "-f",
            "{{.NetworkSettings.Networks.kind.IPAddress}}",
            &container_name,
        ],
    )
    .context("inspecting control-plane container IP")?;
    if inspect_ip.is_empty() {
        bail!("control-plane container IP was empty");
    }
    let endpoint = format!("https://{inspect_ip}:2379");
    eprintln!("Using etcd endpoint: {endpoint}");

    pipe_cmd(
        "cargo",
        &["run", "-p", "etcdetcetc", "--", "crds"],
        "kubectl",
        &["apply", "-f", "-"],
    )
    .context("installing CRDs")?;

    let ca_crt_str = ca_crt.to_string_lossy().into_owned();
    let tls_crt_str = tls_crt.to_string_lossy().into_owned();
    let tls_key_str = tls_key.to_string_lossy().into_owned();
    let from_ca = format!("ca.crt={ca_crt_str}");
    let from_crt = format!("tls.crt={tls_crt_str}");
    let from_key = format!("tls.key={tls_key_str}");
    pipe_cmd(
        "kubectl",
        &[
            "create",
            "secret",
            "generic",
            "etcd-root",
            "--namespace",
            "default",
            "--from-file",
            &from_ca,
            "--from-file",
            &from_crt,
            "--from-file",
            &from_key,
            "--dry-run=client",
            "-o",
            "json",
        ],
        "kubectl",
        &["apply", "-f", "-"],
    )
    .context("creating/updating etcd-root secret")?;

    let etcd_cluster_cr = format!(
        r#"{{
  "apiVersion": "etcdetcetc.samcday.com/v1alpha1",
  "kind": "EtcdCluster",
  "metadata": {{"name": "dev", "namespace": "default"}},
  "spec": {{
    "endpoints": ["{endpoint}"],
    "authSecretRef": {{"name": "etcd-root"}}
  }}
}}"#
    );
    run_with_stdin("kubectl", &["apply", "-f", "-"], etcd_cluster_cr.as_bytes())
        .context("creating/updating EtcdCluster dev")?;

    println!("export KUBECONFIG={}", shell_single_quote(&kubeconfig_str));
    eprintln!("dev-up complete");
    Ok(())
}

fn cmd_dev_down() -> Result<()> {
    eprintln!("Starting dev-down...");

    ensure_tool("kind")?;

    let clusters =
        run_stdout("kind", &["get", "clusters"]).context("checking existing kind clusters")?;
    let cluster_exists = clusters.lines().any(|line| line.trim() == CLUSTER_NAME);
    if cluster_exists {
        run("kind", &["delete", "cluster", "--name", CLUSTER_NAME])
            .context("deleting kind cluster")?;
    } else {
        eprintln!("Kind cluster '{CLUSTER_NAME}' does not exist, skipping delete");
    }

    let dev_dir = std::env::current_dir()
        .context("getting current directory")?
        .join(DEV_DIR);
    if dev_dir.exists() {
        eprintln!("+ rm -rf {}", dev_dir.display());
        fs::remove_dir_all(&dev_dir).with_context(|| format!("removing {}", dev_dir.display()))?;
    } else {
        eprintln!("{} does not exist, skipping", dev_dir.display());
    }

    eprintln!("dev-down complete");
    Ok(())
}

fn ensure_tool(name: &str) -> Result<()> {
    let status = Command::new(name)
        .arg("--help")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();

    match status {
        Ok(_) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            bail!("required tool '{name}' not found on PATH")
        }
        Err(err) => Err(err).with_context(|| format!("checking tool '{name}' on PATH")),
    }
}

fn run(program: &str, args: &[&str]) -> Result<()> {
    eprintln!("+ {}", format_command(program, args));
    let output = Command::new(program)
        .args(args)
        .output()
        .with_context(|| format!("spawning command: {}", format_command(program, args)))?;

    if !output.stdout.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&output.stdout));
    }
    if !output.stderr.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
    }

    if !output.status.success() {
        bail!(
            "command failed ({}): {}",
            output
                .status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string()),
            format_command(program, args)
        );
    }

    Ok(())
}

fn run_stdout(program: &str, args: &[&str]) -> Result<String> {
    eprintln!("+ {}", format_command(program, args));
    let output = Command::new(program)
        .args(args)
        .output()
        .with_context(|| format!("spawning command: {}", format_command(program, args)))?;

    if !output.stderr.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
    }

    if !output.status.success() {
        if !output.stdout.is_empty() {
            eprint!("{}", String::from_utf8_lossy(&output.stdout));
        }
        bail!(
            "command failed ({}): {}",
            output
                .status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string()),
            format_command(program, args)
        );
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn pipe_cmd(
    cmd1_program: &str,
    cmd1_args: &[&str],
    cmd2_program: &str,
    cmd2_args: &[&str],
) -> Result<()> {
    eprintln!(
        "+ {} | {}",
        format_command(cmd1_program, cmd1_args),
        format_command(cmd2_program, cmd2_args)
    );

    let mut cmd1 = Command::new(cmd1_program)
        .args(cmd1_args)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .with_context(|| {
            format!(
                "spawning command: {}",
                format_command(cmd1_program, cmd1_args)
            )
        })?;

    let cmd1_stdout = cmd1
        .stdout
        .take()
        .context("taking stdout for piped command")?;

    let cmd2_output = Command::new(cmd2_program)
        .args(cmd2_args)
        .stdin(Stdio::from(cmd1_stdout))
        .output()
        .with_context(|| {
            format!(
                "spawning command: {}",
                format_command(cmd2_program, cmd2_args)
            )
        })?;

    let cmd1_status = cmd1.wait().context("waiting for first piped command")?;

    if !cmd2_output.stdout.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&cmd2_output.stdout));
    }
    if !cmd2_output.stderr.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&cmd2_output.stderr));
    }

    if !cmd1_status.success() {
        bail!(
            "first piped command failed ({}): {}",
            cmd1_status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string()),
            format_command(cmd1_program, cmd1_args)
        );
    }

    if !cmd2_output.status.success() {
        bail!(
            "second piped command failed ({}): {}",
            cmd2_output
                .status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string()),
            format_command(cmd2_program, cmd2_args)
        );
    }

    Ok(())
}

fn run_with_stdin(program: &str, args: &[&str], stdin_bytes: &[u8]) -> Result<()> {
    eprintln!("+ {}", format_command(program, args));

    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .with_context(|| format!("spawning command: {}", format_command(program, args)))?;

    {
        let stdin = child.stdin.as_mut().context("opening command stdin")?;
        stdin
            .write_all(stdin_bytes)
            .context("writing command stdin")?;
    }

    let output = child.wait_with_output().context("waiting for command")?;

    if !output.stdout.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&output.stdout));
    }

    if !output.status.success() {
        bail!(
            "command failed ({}): {}",
            output
                .status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string()),
            format_command(program, args)
        );
    }

    Ok(())
}

fn format_command(program: &str, args: &[&str]) -> String {
    if args.is_empty() {
        return program.to_owned();
    }
    format!("{} {}", program, args.join(" "))
}

fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}
