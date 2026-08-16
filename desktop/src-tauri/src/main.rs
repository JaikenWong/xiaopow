// XiaoPaw 桌面外壳 — 负责启动/守护 Python sidecar 子进程

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const API_PORT: u16 = 9191;

/// sidecar 句柄：正常 spawn 拿到 CommandChild；
/// 复用残留实例时只有 pid（读自 xiaopaw.pid），退出时按 pid kill。
struct SidecarState {
    child: Option<CommandChild>,
    reused_pid: Option<u32>,
}

impl SidecarState {
    fn new() -> Self {
        Self { child: None, reused_pid: None }
    }
}

/// 退出时确保 sidecar 进程被 kill（防止残留进程占用 9191）。
fn kill_sidecar(state: &mut SidecarState) {
    if let Some(child) = state.child.take() {
        let _ = child.kill();
        eprintln!("[xiaopaw] sidecar killed on app exit");
        return;
    }
    if let Some(pid) = state.reused_pid {
        // 复用的残留实例没有 CommandChild，按 pid kill
        #[cfg(unix)]
        let ok = std::process::Command::new("kill")
            .args(["-9", &pid.to_string()])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        #[cfg(windows)]
        let ok = std::process::Command::new("taskkill")
            .args(["/F", "/PID", &pid.to_string()])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if ok {
            eprintln!("[xiaopaw] reused sidecar (pid {pid}) killed on app exit");
        } else {
            eprintln!("[xiaopaw] failed to kill reused sidecar pid {pid}");
        }
    }
}

/// 探测 sidecar 是否已在运行（端口 9191 已监听）。
/// 已有一个 sidecar 在跑时直接复用，避免重复 spawn 导致端口冲突崩溃。
fn sidecar_already_running() -> bool {
    std::net::TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], API_PORT)),
        Duration::from_millis(300),
    )
    .is_ok()
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Mutex::new(SidecarState::new()))
        .setup(|app| {
            // 配置文件放在应用数据目录（用户可写，不随安装包只读）
            let config_dir = app.path().app_data_dir()?;
            std::fs::create_dir_all(&config_dir)?;
            let config_path = config_dir.join("config.yaml");

            // 已有 sidecar 在跑（如旧实例残留）-> 复用，不再 spawn。
            // 必须能读到 xiaopaw.pid 才复用（确认为本目录的 XiaoPaw sidecar，
            // 而非恰好占用 9191 的其他进程），且记录 pid 供退出时 kill。
            if sidecar_already_running() {
                match std::fs::read_to_string(config_dir.join("xiaopaw.pid"))
                    .ok()
                    .and_then(|s| s.trim().parse::<u32>().ok())
                {
                    Some(pid) => {
                        eprintln!(
                            "[xiaopaw] sidecar already running on port {API_PORT} (pid {pid}), reuse it"
                        );
                        *app.state::<Mutex<SidecarState>>().lock().unwrap() =
                            SidecarState { child: None, reused_pid: Some(pid) };
                        return Ok(());
                    }
                    None => {
                        // 端口被占用但读不到 pid：不是本目录的 XiaoPaw sidecar，
                        // 可能是其他进程占用。不静默 spawn（sidecar 绑定会失败），
                        // 直接报错，让用户处理后重试。
                        return Err(format!(
                            "端口 {API_PORT} 被占用且未找到 xiaopaw.pid，\
                             可能被其他进程占用，请结束后重试。"
                        )
                        .into());
                    }
                }
            }

            // 启动 Python sidecar（PyInstaller 打包产物，放在 src-tauri/binaries/）
            let sidecar = app
                .shell()
                .sidecar("xiaopaw")
                .map_err(|e| e.to_string())?;

            let (mut rx, child) = sidecar
                .args([
                    "--config",
                    config_path.to_str().unwrap_or_default(),
                    "--api-port",
                    &API_PORT.to_string(),
                ])
                .spawn()
                .expect("failed to spawn XiaoPaw sidecar");

            // 保存 child handle 到 state，供退出时 kill
            *app.state::<Mutex<SidecarState>>().lock().unwrap() =
                SidecarState { child: Some(child), reused_pid: None };

            // 后台转发 sidecar 输出到日志（便于排查启动问题）
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            print!("[xiaopaw] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            eprint!("[xiaopaw] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(payload) => {
                            eprintln!("[xiaopaw] exited: {:?}", payload.code);
                            break;
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building XiaoPaw desktop")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                // 退出时 kill sidecar（自有 child 或复用的 pid），防止残留进程占用 9191
                let state = app.state::<Mutex<SidecarState>>();
                kill_sidecar(&mut state.lock().unwrap());
            }
        });
}
