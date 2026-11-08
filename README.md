# APK2PC Studio

> 把 APK **解包 → 还原 Java 源码 → 重建签名 → 装进电脑里的安卓模拟器跑起来**，一条流水线全自动。
> 程序只是辅助，**输出的成品才是重点**：改过的包能重新打包、能装、能生成双击即用的单文件启动器。

![在 Windows 里跑起来的重建包](docs/重建包运行.png)

**三种界面，随你挑**（同一个核心，功能完全一致）：

| 界面 | 长什么样 | 适合谁 |
|---|---|---|
| **命令行向导** | 黑框菜单，输入数字 / 拖文件 | 默认，最省资源，服务器也能用 |
| **浏览器界面** | 自动打开本地网页，能看进度条和日志 | 想看清每一步 |
| **图形窗口** | 像普通软件一样的窗口，能点按钮 | 不习惯命令行的人 |

> 先说清边界：**APK 不能编译成原生 Windows EXE**。它是 Dalvik/ART 字节码 + Android 框架，
> 离开 Android 运行时就跑不了。本工具的做法是「重建签名后的 APK + 自动托管的安卓模拟器 +
> 单文件启动器」，让你在电脑上双击就能用，而不是假装把它变成了 PE 程序。

---

## 目录

- [下载哪个](#下载哪个)
- [三分钟上手](#三分钟上手)
- [它到底做什么](#它到底做什么)
- [工作原理](#工作原理)
- [安装与环境](#安装与环境)
- [使用](#使用)
- [配置](#配置)
- [产物与目录](#产物与目录)
- [常见问题](#常见问题)
- [从源码构建](#从源码构建)
- [合规](#合规)

---

## 下载哪个

去 [Releases](https://github.com/Zhichuang2/apk2pc-studio/releases) 下 **v1.2.0**，共 6 个资产：

| 资产 | 内容 | 谁该下 |
|---|---|---|
| `...-AllInOne.zip` | **三合一**：3 个专用 exe + 界面选择器 + 说明 | 想都要 / 第一次用 |
| `...-CLI.exe` | 命令行向导版（最轻量） | 只要黑框菜单 |
| `...-Web.exe` | 浏览器界面版 | 只要网页界面 |
| `...-GUI.exe` | 图形窗口版（tkinter） | 只要能点按钮的窗口 |
| `...-Source.zip` | 源码 | 想看代码 / 二开 |
| `...-Source.tar.gz` | 源码 | Linux / macOS 下查看 |

**都不需要装 Python**，双击即用。三合一包里的 `APK2PCStudio.exe` 是界面选择器，
双击后按 `1`/`2`/`3` 选一种；之后想固定用某个界面，直接双击对应的专用 exe。

单文件 exe 约 10 MB，但**安卓运行时约 2 GB 是首次运行时才下载**，而且所有版本、
所有生成的启动器**共享同一份**（见[工作原理](#工作原理)）。

---

## 三分钟上手

1. 双击 exe（三合一包里选 `1` 或直接回车）。
2. 第一次会问「安卓模拟器装在哪」——**直接回车用默认位置**即可，之后会写入 PATH，
   下次不再问；也可以把已有 SDK 目录拖进来复用。
3. 把 `.apk` 拖进窗口 / 粘贴完整路径 → 一键全流程。

跑完你会得到：解包目录、Java 源码、一份重建签名的可安装 APK，以及模拟器里已经打开的应用。

> 想改东西？解包出的 `src\` 里可以改 smali / 资源，改完选「重新打包签名」就出新包。
> 这就是「先拆出来是为了方便重新打包」，不是把原包原样套回去。

---

## 它到底做什么

原本要手敲、且每一步都有坑的一串命令：

```
java -jar apktool.jar d app.apk        # 资源 / smali 解包
jadx app.apk                           # dex → 可读 Java（要配依赖树）
java -jar apktool.jar b src            # 重建，参数版本差异大
zipalign -p 4 ...                      # 4 字节对齐
apksigner sign --ks ...                # v1/v2/v3 签名，还要自己造密钥
adb install -r -t -g ...               # 签名冲突 / 版本降级 / 空间不足都要手工处理
emulator -avd ... && adb shell am start -n <包名>/<启动Activity>
```

本工具把这些串成**六步流水线**，并且自动处理：工具链下载、包名与启动 Activity 识别、
签名冲突自动卸载重装、版本降级、模拟器数据分区不足、快照快速开机、中间副本清理。

**能力一览**

| | |
|---|---|
| 解包 | apktool 3.0.3，资源 + smali，自动识别包名 / 启动 Activity / 版本 |
| Java | jadx 1.5.1（Maven 中央仓解析依赖树，不依赖 GitHub）；装不上自动回落 androguard |
| 重建 | apktool + aapt2；`src\` 改过之后可反复重打包 |
| 签名 | zipalign 4 字节对齐 + apksigner v1/v2/v3，自签后自动验签 |
| 运行 | 自动装配 Android 模拟器（WHPX）、快照开机、adb 装机并拉起 |
| 启动器 | 把**重建签名后的 APK** 内嵌进 exe 尾部，双击即开机装包拉起 |
| 运行时共享 | 先查 PATH / 环境变量，找不到才询问；失效旧记录自动清除；不重复下载 |
| 产物 | 只保留 1 份签名成品，中间副本自动删；输出目录可切换 |
| 界面 | 命令行 / 浏览器 / 图形窗口，随时可取消（真杀子进程） |
| 依赖 | **零第三方 Python 库**，纯标准库；界面是本地网页 |

---

## 工作原理

### 六步流水线

```
 APK ──① 解包──> src/ (smali + res + AndroidManifest.xml)
      ──② Java──> java/  (jadx / androguard)
      ──③ 重建──> unsigned.apk      ← src/ 可改完再来
      ──④ 签名──> signed.apk  (zipalign + v1/v2/v3)
      ──⑤ 装机──> 模拟器
      ──⑥ 拉起──> 应用主界面
                    └─> 成品输出目录 output/（可切换）
```

每一步都是独立任务，可以单独执行（只拆包 / 只出 Java / 重新打包 / 直接运行）。

### 为什么必须重新签名

apktool 重建出来的包没有签名；Android 7+ 的 v2 签名要求 zip 条目按 4 字节对齐，
顺序错了会直接装不上。所以流程里固定是 `zipalign` → `apksigner`，并用自生成的
PKCS12 密钥库（RSA 2048 / 10000 天 / 别名 `apk2pc`）。**因此覆盖安装原版会签名冲突**，
程序检测到后会先卸载旧包再装。

### 单文件启动器

```
[ 主程序 exe ][ 重建签名后的 APK ][ JSON 配置 ][ 长度 ][ MAGIC ]
```

启动时从尾部读出配置与 APK，解到数据目录，然后「检查共享运行时 → 装机 → 拉起」。
它**不启 HTTP 服务、不装 apktool/jadx/build-tools**，只负责跑这一个应用，所以很轻。
内嵌的是重建签名后的包，不是原包。

### 运行时只下载一次

参考 APK2EXE 的思路，但把「共享」做全了：

1. 每次运行先查用户 PATH、`APK2PC_RUNTIME`、`APK2PC_TOOLPATHS`；
2. 命中就直接复用，**不下载、不询问**；
3. 旧记录指向的目录已不存在 → 自动删掉这条记录，重新询问位置；
4. 还没装 → 询问装在哪，确认后写入用户级环境变量；
5. 自定义目录（名字里不带项目名）也能正确登记复用，重复调用幂等。

### 技术栈

| 层 | 用到 |
|---|---|
| 后端 | Python 3.10+ 标准库（`http.server` / `subprocess` / `threading`） |
| 界面 | 命令行向导、本地 Web（原生 HTML/CSS/JS，零依赖）、tkinter 桌面窗口 |
| 打包 | PyInstaller（`--onefile`；GUI 版用带 tkinter 的完整 Python） |
| 外部工具 | apktool / jadx / androguard / build-tools(34) / platform-tools / emulator |

---

## 安装与环境

**直接下 exe 就不用装 Python**。若要跑源码，需要 Python 3.10+（GUI 版还需 tkinter）。

| 要求 | 说明 |
|---|---|
| 系统 | Windows 10 / 11 x64 |
| JDK | **11+**（apktool 3.x / apksigner 强制要求） |
| 虚拟化 | BIOS 开 VT-x/AMD-V，并在「启用或关闭 Windows 功能」勾选 **Windows 虚拟机监控程序平台 (WHPX)** |
| 磁盘 | ≥ 15 GB 空闲（模拟器镜像约 2 GB；大包重建时还需要几倍临时空间） |
| 网络 | 首次运行需联网拉工具链（Maven 中央仓 + dl.google.com，国内可达） |

首次装配会下载：apktool(≈23 MB) → jadx 依赖树(≈30 MB) → build-tools(≈60 MB)
→ platform-tools + emulator + 系统镜像(≈2 GB)。**一次性**，位置写入 PATH 后全局共享。

---

## 使用

### 三种界面

| 界面 | 怎么进 |
|---|---|
| 命令行向导 | 双击 `...-CLI.exe`，或三合一选择器按 `1` |
| 浏览器界面 | 双击 `...-Web.exe`（自动开网页），或任意 exe 加 `--web` |
| 图形窗口 | 双击 `...-GUI.exe`，或三合一选择器按 `3` |

也能用环境变量强制指定：`APK2PC_MODE=cli|web|gui|select`。

### 局部操作

| 想做的事 | 选哪个 |
|---|---|
| 一路跑完（拆包→源码→重建→签名→装机→打开） | 一键跑起来 |
| 只想看 / 改代码 | 只拆包 + Java |
| 改完 `src\` 出新包 | 重新打包签名 |
| 只检查/补装工具，不跑模拟器 | 环境检查 |
| 把成品做成双击即用的单文件 exe | 生成单文件启动器 |
| 切换成品输出目录 | 输出位置（命令行菜单 6 / 图形界面第 2 步） |
| 删中间副本腾磁盘 | 清理无用数据 |

### 命令行参数（进阶）

```bat
APK2PCStudio.exe                 :: 默认界面（按 exe 文件名决定）
APK2PCStudio.exe --cli          :: 强制命令行向导
APK2PCStudio.exe --web          :: 强制浏览器界面
APK2PCStudio.exe --gui          :: 强制图形窗口（需带 tkinter 的构建）
APK2PCStudio.exe --select       :: 弹界面选择菜单
APK2PCStudio.exe --web --port 8788 --no-browser
APK2PCStudio.exe --launch "D:\app.apk"        :: 直接装并运行某个 APK
APK2PCStudio.exe --headless-install           :: 只装配工具链并注册 PATH
```

快捷键（浏览器界面）：`Ctrl+Enter` 一键全流程，`Esc` 取消。

---

## 配置

设置保存在 `%LOCALAPPDATA%\APK2PCStudio\settings.json`，界面里改完自动保存。

| 字段 | 默认值 | 作用 |
|---|---|---|
| `avd` | `apk2pc_x86` | 模拟器 AVD 名称 |
| `system_image` | `...android-34...x86_64` | 系统镜像包名 |
| `extra_apktool_args` | `""` | 传给 apktool 的额外参数 |
| `emu_extra_args` | `-no-boot-anim` | 模拟器启动参数（默认允许快照快启） |
| `quick_boot` | `true` | 允许从快照开机；起不来自动冷启重建 |
| `jadx_threads` | `4` | 反编译线程数 |
| `output_dir` | `""` | **成品输出目录**；空 = 程序目录下 `output\` |
| `auto_start_emulator` | `true` | 需要时自动拉起模拟器 |
| `last_apk` / `last_signed` / `last_workspace` | 自动 | 记住上次路径，下次直接回车复用 |

**环境变量**

| 变量 | 作用 |
|---|---|
| `APK2PC_HOME` | 覆盖数据目录（工具链 / 工作区 / 密钥库都放这） |
| `APK2PC_RUNTIME` | 安卓 SDK 根目录；由程序自动登记，也可手动指定已有 SDK |
| `APK2PC_TOOLPATHS` | 程序加入 PATH 的目录清单，用于精确复用与失效清理 |
| `APK2PC_OUTPUT` | 成品输出目录（等价于 `output_dir`） |
| `APK2PC_MODE` | 强制界面：`cli` / `web` / `gui` / `select` |
| `JAVA_HOME` | JDK 路径；程序会自动修正只指向 javapath 壳目录的情况 |

**签名密钥**：`%LOCALAPPDATA%\APK2PCStudio\apk2pc-debug.keystore`
（PKCS12 / RSA 2048 / 10000 天 / 别名 `apk2pc` / 口令 `android`）。仅用于本地调试安装，
不要用在生产发布。日志写到 `%LOCALAPPDATA%\APK2PCStudio\logs\`。

---

## 产物与目录

一次全流程后（`<输出目录>/<应用名>/`）：

```
output/
└─ <应用名>/
   ├─ <应用名>-signed.apk        ← 唯一保留的可安装成品
   └─ <应用名>.exe               ← 可选：内嵌该包的单文件启动器
```

工作区（默认 `%LOCALAPPDATA%\APK2PCStudio\workspace\<应用名>_<时间>\`）里是过程文件：

```
src/            apktool 解包结果（smali / res / AndroidManifest.xml，可改）
java/           jadx 反编译的 Java 源码
gradle-project/ 可选：Android Studio 工程骨架（AGP 8，模块 :app）
launcher/       启动器产物
```

签名成功后 `unsigned.apk` / `aligned.apk` / `.idsig` **自动删除**，
2 GB 级的包能省 4–5 GB 磁盘，只留一份成品。

仓库结构：

```
app.py / gui_main.py     命令行与图形化版入口
gui_app.py               tkinter 桌面窗口
core/                    配置 / 流水线 / 装配 / 运行时 / 服务 / 环境变量 / 启动器
web/                     本地网页界面（零依赖）
build_exe.py             打单个 exe
build_release.py         一次产出全部 6 个发布资产
tests/e2e.py             端到端测试
docs/                    运行截图
```

---

## 常见问题

<details><summary><b>双击后黑框闪一下就没了</b></summary>

v1.2.0 起顶层有异常兜底：任何启动期错误都会打印中文原因 + 堆栈并**停住等你按回车**，
不会静默退出。若仍闪退，到 `%LOCALAPPDATA%\APK2PCStudio\logs\` 看日志，
或在 cmd 里运行 exe 把输出截图给我。
</details>

<details><summary><b>一直显示「等待开机… 无设备」</b></summary>

模拟器进程秒退了。常见原因：勾选了「模拟器全屏」（部分机器 + swiftshader 会直接崩，
**默认已不勾**）、WHPX 没启用、C 盘空间不足建不出数据分区。
程序现在会把模拟器输出写到 `logs/emulator-*.log`，并把失败原因直接打在界面上；
全屏失败会自动改窗口模式重试一次。
</details>

<details><summary><b>装包报 INSUFFICIENT_STORAGE / 签名冲突 / VERSION_DOWNGRADE</b></summary>

- 空间不足：大包需要更大的 AVD 数据分区（默认 8G）；清一下 C 盘回收站再冷启。
- 签名冲突 / 版本降级：程序会**自动卸载旧包再装**，无需手工处理。
</details>

<details><summary><b>解包后 smali 是空的 / 没有 Java 源码</b></summary>

大概率是**加固壳**（360 / 乐固 / 梆梆 / 爱加密）：dex 被加密，需要先脱壳。
本工具不提供脱壳能力。另外 jadx 若缺 jar 会静默失败，程序装配后会自检 `--version`，
不通过则自动回落到 androguard。
</details>

<details><summary><b>Java 源码能直接 gradle build 吗</b></summary>

不能。反编译产物含混淆、内联、资源 id、native 依赖，`gradle-project/` 只是方便阅读与
对照的骨架，不是可编译工程。真正能装能跑的是**重建签名的 APK**。
</details>

<details><summary><b>换台电脑 / 换输出目录，运行时要重下吗</b></summary>

同一台电脑不用：位置已写进用户环境变量，所有版本与启动器共享。
换电脑时会重新询问位置，指到已有的安卓 SDK 目录即可复用，不会重复下载。
</details>

<details><summary><b>中文路径 / 中文应用名</b></summary>

支持。控制台强制 UTF-8，界面与文件名都做了清洗与转义。
</details>

---

## 从源码构建

```bat
python build_exe.py                :: 单个 exe（命令行/浏览器/选择器共用）
python build_exe.py --onedir       :: 目录形式
python build_release.py            :: 一次产出 6 个发布资产
```

`build_release.py` 需要系统里有一份**带 tkinter** 的完整 Python（3.13/3.14）来打 GUI 版，
且 PyInstaller ≥ 6.22.3（旧版无法正确收集 Tcl/Tk 9 的数据目录）。源码包由 `git archive`
生成，天然排除 `dist/`、`build/`、`*.apk` 与密钥。

已实测：2.47 GB 的真实游戏包重建 + 自签名后装机成功并进入主界面；
单文件 exe 冷启动约 0.7 s；全流程中间副本自动清理释放 4.62 GB。

---

## 合规

仅用于**你自己拥有合法授权**的应用（自研、开源、内部包、有授权的二次开发）。
本工具不提供破解、脱壳、绕过授权或规避签名校验的能力。
第三方工具的许可归各自项目所有（apktool / jadx / Android SDK 等）。

---

## 参考

- [APK2EXE](https://github.com/doxo46/APK2EXE-) —— 共享运行时 + 轻量启动器的思路
- [apktool](https://apktool.org/) · [jadx](https://github.com/skylot/jadx) ·
  [androguard](https://github.com/androguard/androguard)
