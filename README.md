# APK2PC Studio

**把一个 APK 从「装不进去的二进制」变成「能在 Windows 上跑起来、还能改」的完整工程。**

一条流水线做完全部脏活：apktool 解包 → jadx 反编译出可读 Java → 改完重建 → 对齐签名 → 装进电脑里的 Android 模拟器并拉起。
界面是本地网页（零第三方依赖），可打包成单个 EXE 双击即用。

`Windows 10/11` · `Python 3.9+（仅源码运行需要）` · `JDK 11+` · 冷启动 `0.68 s`


---

## 目录

- [它解决什么问题](#它解决什么问题)
- [特性](#特性)
- [工作原理](#工作原理)
- [安装](#安装)
- [使用](#使用)
- [配置](#配置)
- [产物与目录结构](#产物与目录结构)
- [常见问题](#常见问题)
- [兼容性与实测](#兼容性与实测)
- [合规声明](#合规声明)

---

## 它解决什么问题

逆向一个 APK，命令通常是这样的七零八落：

```bat
apktool d app.apk -o src
jadx -d java --no-res app.apk
apktool b src -o unsigned.apk
zipalign -f 4 unsigned.apk aligned.apk
apksigner sign --ks my.keystore --out signed.apk aligned.apk
adb install -r signed.apk
adb shell am start -n pkg/.MainActivity
```

每一步都有自己的坑：apktool 3.x 删掉了 `--use-aapt2`、jadx 少一个 `logback-core` 就 **rc=1 且零输出**、`JAVA_HOME` 指向 javapath 壳目录会让 apksigner 全挂、Android 11+ 不签 v2 根本装不上、签名冲突要先卸旧包、2 GB 的包还要先把 AVD 数据分区调大……

APK2PC Studio 把这些坑**固定在代码里**，你只面对一个界面和一条进度条。

### 先说清楚：APK 不能「编译」成原生 EXE

APK 是 dex 字节码 + Android 框架 + JNI 原生库，Windows 上没有任何编译器能把它们翻译成原生 EXE —— 这不是工具链选型问题，是运行时模型根本不同。

市面上所有「APK 转 EXE」产品，本质都是**把 Android 运行时塞进电脑**，EXE 只是壳。本工具用的是同一条路，但把壳做薄：

- **容器**：自动装配一台 Android 14 x86_64 模拟器（`google_apis`），APK 真的跑在 Windows 里；
- **壳**：可选的「单文件启动器」—— APK 直接内嵌进 exe 尾部，双击 → 开机 → 装包 → 拉起，旁边不用再放 apk。

想要原生性能只有两条路（本工具不代做）：Android-x86 / BlissOS 装物理机或虚拟机；或在有源码的前提下用 Flutter / React Native / Qt 重写。

---

## 特性

| 能力 | 说明 |
|---|---|
| **全流程一键跑** | 六步自动串联，日志实时滚动，随时可取消（会真的杀掉子进程，不留僵尸） |
| **Java 源码还原** | jadx 优先；jadx 装不上时自动回落到 androguard（内置 DAD 反编译器） |
| **工具链自装配** | apktool / jadx / build-tools / 模拟器都不入库，缺失时点一下自动下载 |
| **国内可用的源** | apktool、jadx 走 **Maven 中央仓**（GitHub 直连在国内常年不通，这里不用赌） |
| **自签名可安装** | zipalign + apksigner，同时开 v1/v2/v3；自动生成并复用调试密钥 |
| **签名冲突自愈** | 遇到 `UPDATE_INCOMPATIBLE` 自动卸载旧包再装，不用你手动 `pm uninstall` |
| **单文件启动器** | APK 内嵌 exe 尾部，双击即用；产物是**一个文件** |
| **快照快速开机** | 第二次起走快照，模拟器开机从 90 s 级降到 10 s 级；快照坏了自动冷启重建 |
| **可导出 Android Studio 工程** | 生成 AGP 8 骨架（模块 `:app`、`namespace`、`compileSdk 34`），把 smali/res/assets/jniLibs 摆到正确位置 |
| **局部操作** | 只解包、只反编译、只重建签名、只运行 —— 不用每次跑完整条链 |
| **零第三方 Python 依赖** | 只用标准库；界面是一个本地 HTTP 服务 + 原生 HTML/JS/CSS |

---

## 工作原理

### 流水线

```
                 ┌──────────────────────────────────────────────────┐
   你的 APK ───► │ ①  准备     建工作区、解析 manifest/包名/ABI/壳  │
                 │ ②  解包     apktool d  →  res/ + smali/ + assets │
                 │ ③  Java     jadx -d    →  可读 .java（失败回落） │
                 │ ④  重建     apktool b  →  unsigned.apk (aapt2)   │
                 │ ⑤  签名     zipalign → apksigner v1/v2/v3        │
                 │ ⑥  运行     emulator + adb install + am start    │
                 └──────────────────────────────────────────────────┘
                                      │
                                      ▼
                        电脑屏幕上真的出现那个 App
```

每一步都有独立的成功/跳过/失败状态，失败会带上可读原因（不是甩一坨 stderr）。
第 ③ 步是**非阻塞**的：加固包反编译不出来，流程照样能走到 ④⑤⑥ —— 因为重建只依赖 ②。

### 为什么签名这一步不能省

Android 7 起要求 v2 签名（整个 APK 的哈希树），11 起对 v2 的校验更严。apktool 重建出来的包**签名必然失效**（内容变了），不重签连安装都过不去。所以本工具固定做三件事：

1. `zipalign -f 4` —— 4 字节对齐，否则 `INSTALL_PARSE_FAILED_NOT_ALIGNED`；
2. `apksigner sign --v1 --v2 --v3` —— 三套签名全开；
3. `apksigner verify --print-certs` —— 签完自己验一遍，失败就当场报。

### 单文件启动器的内嵌格式

APK 被**追加**在 exe 末尾，前面是可执行体，读取时从尾部定位：

```
[ ────── 原始 exe ────── ][ ──── APK ──── ][ JSON 配置 ][ json 长度 ][ MAGIC ]
                                                                 ↑ 从后往前找
```

好处是：写出时只做 `copy exe` + `append`，改配置时只重写尾部几百字节（2 GB 的 exe 不用重新复制一遍），
exe 本身仍然是合法 PE，双击照常启动。解包逻辑会先读尾部 MAGIC，读不到就当普通模式启动 —— 所以同一个 exe 既能当工具，也能当某个 App 的启动器。

### 技术栈

| 层 | 实现 |
|---|---|
| 界面 | 原生 HTML/CSS/JS，由本地 `http.server` 提供；无框架、无构建步骤 |
| 后端 | Python 标准库（`http.server` / `subprocess` / `threading` / `urllib`） |
| 任务模型 | 内存中的 Job 队列，日志带游标增量拉取（前端只取新增部分，不刷全量） |
| 外部工具 | apktool 3.0.3 · jadx 1.5.1 · build-tools 34.0.0 · Android Emulator + android-34 镜像 |
| 打包 | PyInstaller（onedir 默认 / onefile 可选） |

---

## 安装

### 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10 / 11 x64 | 依赖 WHPX 硬件虚拟化跑模拟器 |
| JDK | **11 或更高**（实测 JDK 21） | apktool 3.x 强制要求；`java`、`keytool` 需在 PATH |
| Python | 3.9+ | **仅源码运行 / 自行打包时需要**，直接用 EXE 不需要 |
| 磁盘 | ≥ 15 GB 空闲（C 盘） | 模拟器镜像 + 系统镜像约 2 GB，2 GB 级 APK 的解包产物更大 |
| 虚拟化 | BIOS 开启 VT-x/AMD-V，并启用「Windows 虚拟机监控程序平台」 | 否则模拟器起不来 |

> 磁盘提示：AVD 镜像和解包产物都很大。C 盘吃紧时建议把成品和中间产物放别的盘；`disk.dataPartition.size` 默认 8G，装 2 GB+ 的包刚好够。

### 方式一：直接用打包好的 EXE（推荐）

从 [Releases](https://github.com/Zhichuang2/apk2pc-studio/releases/latest) 下载，二选一：

| 文件 | 用法 | 冷启动 |
|---|---|---|
| `APK2PCStudio-1.0.0-win64.zip` | 解压到任意目录，双击里面的 `APK2PCStudio.exe` | 0.68 s |
| `APK2PCStudio-1.0.0-onefile.exe` | 单文件，双击就跑，不用解压 | 0.69 s |

双击即开，浏览器自动打开界面。**不需要 Python、不需要 pip install** —— 程序只用标准库。
仓库里也带了本地构建产物：`dist\APK2PCStudio\APK2PCStudio.exe`。

### 方式二：从源码运行

```bat
git clone https://github.com/Zhichuang2/apk2pc-studio.git
cd apk2pc-studio
python app.py
```

浏览器会自动打开 `http://127.0.0.1:<随机端口>/`。

### 方式三：自己打包

```bat
打包EXE.bat              :: onedir（目录形式，启动最快）
打包EXE.bat --onefile    :: 单文件分发（一个文件，方便发给别人）
```

等价于 `python build_exe.py [--onefile]`。产物对比：

| 形态 | 产物 | 冷启动（3 次实测） | 适用 |
|---|---|---|---|
| onedir（默认） | `dist/APK2PCStudio/`（exe + `_internal/`） | **0.68 s** | 自己用，进程干净 |
| onefile | `dist/APK2PCStudio.exe`（单文件，约 9.4 MB） | **0.69 s**（0.66–0.71） | 发给别人，单个文件 |

> 两者启动已经基本没差别 —— 早期 onefile 慢到 2.8 s，是因为那时把 15 MB 的 apktool.jar 打进了包里、
> 每次启动都要解压一遍。现在 apktool 改为首次运行按需下载，这个代价就消失了。
> 唯一的区别：onefile 每次仍要解压到临时目录，磁盘慢或杀软严格的机器上会比 onedir 略久。
>
> 另注：onedir 版本导出「独立启动器」时会把整个目录复制过去（exe 依赖旁边的 `_internal/`）；
> 想要「一个文件里塞着 APK」，请用 `--onefile` 构建后再导出。

### 首次装配工具链

程序**不带**任何第三方二进制，第一次用会显示缺项，点「装配」按需下载到 `%LOCALAPPDATA%\APK2PCStudio\tools\`：

| 组件 | 体积 | 来源 | 用途 |
|---|---|---|---|
| `apktool.jar` | ~23 MB | Maven 中央仓 | 解包 / 重建 |
| `jadx` | ~40 MB | Maven 中央仓（依赖树）+ 镜像兜底 | Java 反编译 |
| `build-tools 34.0.0` | ~58 MB | `dl.google.com` | zipalign / apksigner / aapt2 |
| 模拟器 + android-34 镜像 | ~1.5–2 GB | `dl.google.com` | 在电脑上运行 APK |

命令行也可一次装完（适合 CI 或提前准备）：

```bat
APK2PCStudio.exe --headless-install
```

---

## 使用

### 界面四步

1. **目标 APK** —— 点「浏览…」弹系统文件对话框，或直接粘贴路径回车。
   「解析」先读包名 / dex 数量 / ABI，并**提示是否加固**（找不到 `classes*.dex` 就是典型壳特征）。
2. **工具链** —— 7 项状态一览，缺哪个点哪个旁边的「装配」。
3. **一键全流程** —— 六步跑完；可中途取消。
4. **产物** —— 也可以只做局部：

| 按钮 | 执行步骤 | 典型用途 |
|---|---|---|
| 一键全流程 | ①→⑥ | 我要它跑起来 |
| 仅解包 + Java | ①②③ | 我只想看代码 |
| 仅 Java | ①③ | 已经有解包结果，只补 Java |
| 重建签名 | ④⑤ | 我改过 smali/res，要出新包 |
| 直接运行 | ⑥ | 包是好的，只想装进模拟器 |

快捷键：`Ctrl+Enter` 跑全流程，`Esc` 取消。

### 命令行

```bat
APK2PCStudio.exe                       :: 打开界面
APK2PCStudio.exe --port 8760           :: 固定端口
APK2PCStudio.exe --no-browser          :: 只起服务，不弹浏览器
APK2PCStudio.exe --launch "x.apk"      :: 直接对某个 APK 跑全流程
APK2PCStudio.exe --headless-install    :: 只装配工具链后退出
```

### 生成单文件启动器

在「产物」区点「生成启动器」：把当前 APK 焊进一个 exe，双击 → 自动开机 → 自动装包 → 拉起。
启动器会读**同目录同名**的 `xxx.apk2pc.json`：

```json
{
  "apk": "D:\\games\\demo.apk",
  "label": "demo",
  "package": "com.example.demo",
  "activity": "com.example.demo.MainActivity",
  "avd": "apk2pc_x86",
  "fullscreen": false
}
```

`fullscreen` 默认 `false` —— 部分机器上 `-fullscreen` 会让模拟器**秒退**，程序会自动降级重试一次，但没必要给自己找麻烦。

---

## 配置

### `settings.json`

位置：`%LOCALAPPDATA%\APK2PCStudio\settings.json`（首次运行自动生成）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `avd` | `apk2pc_x86` | 使用的 AVD 名称 |
| `system_image` | `system-images;android-34;google_apis;x86_64` | 自动装配时装哪个系统镜像 |
| `jadx_threads` | `4` | jadx 并发线程数；大包吃 CPU 时可调低 |
| `quick_boot` | `true` | 允许模拟器读写快照（第二次起 10 s 级开机） |
| `emu_extra_args` | `-no-boot-anim` | 追加给 emulator 的参数 |
| `extra_apktool_args` | `""` | 追加给 apktool 的参数，例如 `--no-debug-info` |
| `auto_start_emulator` | `true` | 没有在线设备时自动拉起模拟器 |
| `last_apk` | `""` | 上次处理的 APK 路径（界面自动回填） |

### 环境变量

| 变量 | 作用 |
|---|---|
| `APK2PC_HOME` | 覆盖数据根目录（默认 `%LOCALAPPDATA%\APK2PCStudio`）。想让整个工具跑在移动硬盘 / 别的盘上就设它 |
| `JAVA_HOME` | 程序会主动探测 `java.exe`；探测到 javapath 壳目录会自动修正，但仍建议指向真实 JDK |

### 签名密钥

首次签名会自动生成 `%LOCALAPPDATA%\APK2PCStudio\apk2pc-debug.keystore`：

| 项 | 值 |
|---|---|
| alias | `apk2pc` |
| 密码 | `android` |
| 类型 | PKCS12，RSA 2048，有效期 10000 天 |

> 这是**调试密钥**，公开且固定。它只适合自用/内测；要发布请换自己的密钥并妥善保管（重签后原版覆盖安装会失败，需先卸载）。

### 数据目录总览

```
%LOCALAPPDATA%\APK2PCStudio\
├─ tools\              apktool.jar / jadx-lib / build-tools
├─ android-sdk\        emulator / system-images / platform-tools
├─ workspace\          每次任务一个目录
├─ logs\               模拟器启动日志（模拟器秒退时看这里）
├─ apk2pc-debug.keystore
└─ settings.json
```

---

## 产物与目录结构

一次全流程结束后，工作区是这样：

```
workspace\<应用名>_<月日-时分秒>\
├─ src\                 apktool 解包结果（res/ smali/ assets/ lib/ AndroidManifest.xml）
├─ java\                jadx 反编译的 .java 源码
├─ src-unsigned.apk     apktool 重建产物（未签名，装不上）
├─ src-unsigned-aligned.apk   zipalign 之后（未签名）
├─ src-unsigned-signed.apk    ★ 可直接安装
├─ src-unsigned-signed.apk.idsig
├─ gradle-project\      Android Studio 工程骨架（导出时生成）
└─ launcher\            单文件启动器（导出时生成）
```

仓库本身的结构：

```
apk2pc-studio/
├─ app.py                入口：参数解析 / 启动器模式 / 服务启动
├─ core/
│  ├─ config.py          路径、工具发现、配置、环境快照
│  ├─ provision.py       工具链装配（下载、解压、校验、自检）
│  ├─ maven.py           迷你 Maven 解析器（拉 jadx 依赖树）
│  ├─ apk.py             免解包的 APK 结构解析（包名/dex/ABI/壳提示）
│  ├─ pipeline.py        六步流水线 + Job 管理 + Gradle 导出
│  ├─ runtime.py         模拟器/adb：启动、探活、装机、拉起、截图
│  ├─ launcher.py        单文件启动器：写入/读取/改写内嵌载荷
│  ├─ server.py          本地 HTTP 服务与 API
│  └─ runner.py          子进程执行、流式日志、下载（支持断点续传）
├─ web/                  index.html / style.css / app.js（无框架）
├─ tests/e2e.py          命令行端到端测试（decode / build / run）
└─ docs/                 截图
```

界面与后端之间是普通 HTTP API（`/api/state`、`/api/job`、`/api/export`、`/api/emulator` …），
所以也可以写脚本直接驱动，不经过界面。

---

## 常见问题

<details>
<summary><b>模拟器起不来 / 界面一直显示「无设备」</b></summary>

先看 `%LOCALAPPDATA%\APK2PCStudio\logs\emulator-*.log`。绝大多数是两类：

1. **没开 WHPX**：设置 → 应用 → 可选功能 → 更多 Windows 功能 → 勾「Windows 虚拟机监控程序平台」→ 重启；
2. **加了 `-fullscreen`**：部分机器上会秒退。程序会自动降级重试，但建议直接把全屏关掉。
</details>

<details>
<summary><b>装机报 <code>INSTALL_FAILED_INSUFFICIENT_STORAGE</code></b></summary>

AVD 数据分区不够。改 `~/.android/avd/<avd名>.avd/config.ini`：

```ini
disk.dataPartition.size=8G
```

改完 `adb emu kill` 再启动。注意宿主要能腾出对应空间，否则会报 `Not enough space to create userdata partition`。
</details>

<details>
<summary><b>解包出来 smali 是空的 / Java 一个文件都没有</b></summary>

大概率是加固壳（360、腾讯乐固、梆梆、爱加密）。壳把 dex 加密了，`classes*.dex` 根本不存在，apktool 和 jadx 都无从下手 —— 这不是工具的锅，得先脱壳。

界面在「解析」阶段就会提示 `疑似加固/壳`。
</details>

<details>
<summary><b>jadx 没产出，但退出码是 1 且没有任何报错</b></summary>

典型的**依赖缺失**（例如少了 `logback-core`）。程序在装配后会跑一次 `--version` 自检，失败则标记损坏并回落到 androguard；你可以点「重新装配」修好它。
</details>

<details>
<summary><b>重签名的包装不上，提示签名不一致</b></summary>

只能先卸载原版：`adb uninstall <包名>`。程序在检测到 `INSTALL_FAILED_UPDATE_INCOMPATIBLE` 时会自动这么做。
</details>

<details>
<summary><b>导出的 Gradle 工程能直接 build 吗？</b></summary>

**不能。** jadx 的输出经过混淆还原、内联、资源 id 重排，加上 native 依赖，直接 `gradle build` 几乎必然失败。这个工程是给你**读代码和继续改**的骨架，不是一键出包。
</details>

<details>
<summary><b>中文路径 / 长路径有问题吗？</b></summary>

工作区路径含中文可以正常跑（本机实测路径里就有中文）。但 APK 文件名建议避免空格和特殊符号，某些工具的 stderr 解析会受影响。
</details>

---

## 兼容性与实测

本机环境：**Windows 11 + JDK 21 + Android 14 (API 34) x86_64 模拟器**。

| 环节 | 结果 |
|---|---|
| apktool 解包真实 APK | ✔ 3769 个 smali，包名 / 启动 Activity 自动识别 |
| jadx 反编译 | ✔ 2327 个 `.java`（依赖从 Maven 中央仓解析） |
| apktool 重建 + aapt2 | ✔ 输出 unsigned.apk |
| zipalign + apksigner v1/v2/v3 | ✔ 证书校验通过 |
| 自签名包装进模拟器 | ✔ Success（并自动处理签名冲突） |
| **2.47 GB 真实游戏包重建 + 自签名** | ✔ 装机 Success → 进入游戏主界面 |
| 单文件启动器（APK 内嵌 exe） | ✔ 解出的载荷 SHA-256 与原 APK 一致 |
| 程序冷启动 | ✔ onedir 0.68 s / onefile 0.69 s（三次 0.66–0.71 s） |

**尚未验证**：ARM-only 应用在 x86_64 镜像上的转译表现（取决于镜像自带 ARM 转译层）；Mac/Linux 未适配。

---

## 合规声明

本项目是**逆向工程与打包流程的自动化胶水**，不提供任何破解、去壳、绕过授权的能力。

请仅用于**你自己拥有合法授权**的应用：自研应用、开源应用、公司内部包、你有权修改的二次开发对象。
用它处理第三方商业应用并分发，风险与责任由使用者承担，与本项目无关。

---

## 参考

- [apktool](https://apktool.org/) —— 解包 / 重建的事实标准
- [jadx](https://github.com/skylot/jadx) —— dex → Java 反编译器
- [Android 命令行工具](https://developer.android.com/studio#command-line-tools-only) —— build-tools / platform-tools / emulator
- [APK2EXE](https://github.com/doxo46/APK2EXE-) —— 「EXE = 启动器 + APK」这个思路的先行者（它用 WSA，本项目用 AVD 模拟器）
