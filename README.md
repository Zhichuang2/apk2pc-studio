# APK2PC Studio

把 APK **解包 → 还原 Java 源码 → 重建签名 → 在电脑上跑起来**，一条流水线全自动。
程序只是辅助，**输出的成品才是重点**：改过的包能重新打包、能装、能生成双击即用的单文件启动器。

![在 Windows 里运行](docs/重建包运行.png)

> APK 不能编译成原生 Windows EXE —— 它依赖 Android 运行时。本工具的做法是
> 「重建签名后的 APK + 自动托管的安卓模拟器 + 单文件启动器」，让你在电脑上双击就能用。

---

## 它能做什么

原本要手敲的一串命令，现在一键完成：

```
解包apktool → jadx反编译Java → 重新打包 → zipalign+apksigner签名 → adb装机 → 拉起应用
```

- **解包**：资源 + smali，可改代码和资源；改完能反复重新打包
- **Java 源码**：反编译出可读的 `.java`，方便阅读和对照
- **重建签名**：自动对齐 + v1/v2/v3 签名，产出可安装的 APK
- **运行**：自动装配安卓模拟器并把应用装进去、打开
- **启动器**：把重建签名后的 APK 内嵌进单个 exe，双击即用
- **运行时共享**：安卓运行时只下载一次，所有版本与启动器共用

整个过程会自动处理工具链下载、包名识别、签名冲突、版本降级、中间副本清理。

---

## 三种界面

功能完全一致，只是交互方式不同，随你挑：

| 界面 | 长什么样 | 怎么进 |
|---|---|---|
| 命令行向导 | 黑框菜单，输入数字 / 拖文件 | 双击 `...-CLI.exe` |
| 浏览器界面 | 自动打开本地网页，有进度条和日志 | 双击 `...-Web.exe` |
| 图形窗口 | 像普通软件一样的窗口，能点按钮 | 双击 `...-GUI.exe` |

三合一包里的 `APK2PCStudio.exe` 是界面选择器，双击后按 `1`/`2`/`3` 选一种。
也可以用 `APK2PC_MODE=cli|web|gui|select` 或 `--cli` / `--web` / `--gui` / `--select` 指定。

---

## 下载与安装

去 [Releases](https://github.com/Zhichuang2/apk2pc-studio/releases) 下载，共 6 个资产：

| 资产 | 说明 |
|---|---|
| `...-AllInOne.zip` | **三合一**：3 个专用 exe + 界面选择器 + 说明（第一次用下这个） |
| `...-CLI.exe` / `...-Web.exe` / `...-GUI.exe` | 三个独立界面 |
| `...-Source.zip` / `...-Source.tar.gz` | 源码 |

**都不需要装 Python**，双击即用。

| 要求 | 说明 |
|---|---|
| 系统 | Windows 10 / 11 x64 |
| JDK | 11+ |
| 虚拟化 | BIOS 开启虚拟化，并启用「Windows 虚拟机监控程序平台」(WHPX) |
| 磁盘 / 网络 | ≥ 15 GB 空闲；首次运行需联网下载安卓运行时（约 2 GB，只下一次） |

---

## 三分钟上手

1. 双击 exe（三合一包里按 `1` 或直接回车）
2. 第一次会问「安卓模拟器装在哪」，直接回车用默认位置即可，之后写入环境不再问
3. 把 `.apk` 拖进窗口 / 粘贴路径 → 一键全流程

想改东西：解包出的 `src\` 可以改 smali / 资源，改完选「重新打包签名」出新包。
成品默认放在程序目录下的 `output\`，可在界面里切换。

---

## 配置

设置保存在 `%LOCALAPPDATA%\APK2PCStudio\settings.json`，界面里改完自动保存。

| 常用项 | 作用 |
|---|---|
| `output_dir` | 成品输出目录（空 = 程序目录下 `output\`） |
| `avd` / `system_image` | 模拟器名称与系统镜像 |
| `quick_boot` | 允许快照快速开机 |
| `jadx_threads` | 反编译线程数 |
| `emu_extra_args` / `extra_apktool_args` | 传给模拟器 / apktool 的额外参数 |

环境变量：`APK2PC_HOME`（数据目录）、`APK2PC_RUNTIME`（安卓 SDK 位置）、
`APK2PC_OUTPUT`（成品目录）、`APK2PC_MODE`（强制界面）、`JAVA_HOME`（JDK 路径）。

---

## 从源码运行 / 构建

```bat
python app.py                :: 命令行向导
python app.py --web          :: 浏览器界面
python gui_main.py           :: 图形窗口（需 tkinter）
python build_exe.py          :: 打单个 exe
python build_release.py      :: 一次产出全部发布资产
```

源码只用 Python 标准库，无需 `pip install`。GUI 版需要带 tkinter 的完整 Python 与
PyInstaller ≥ 6.22.3；源码包由 `git archive` 生成，不含构建产物。

---

## 合规

仅用于你自己拥有合法授权的应用（自研、开源、内部包、有授权的二次开发）。
本工具不提供破解、脱壳或绕过授权的能力。第三方工具许可归各自项目所有
（[apktool](https://apktool.org/) · [jadx](https://github.com/skylot/jadx) · Android SDK）。
