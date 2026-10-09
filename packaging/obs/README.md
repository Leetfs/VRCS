# GitHub Actions → OBS → GitHub Release

`main` 收到 push 时，`.github/workflows/release.yml` 自动发布普通版与 CUDA 版。
版本递增末位，例如 `0.2.1 → 0.2.2`；版本同步到 Tauri、npm、Cargo 及锁文件。
工作流先检查 OBS 凭据和签名密钥，再提交版本预留 commit；所有构建与签名成功后创建 tag 并发布 Release。
版本提交使用 `GITHUB_TOKEN`，不会递归触发 push 工作流。失败可重跑并复用版本；新用户提交会预留下一个版本。
并发 push 串行处理，待运行的任务构建当时最新 main，Release 列出自上个发布版本以来的作者和 commit 链接。

## 一次性设置

先登录 `gh auth login`。需要仓库写入、workflow 和管理 Actions Secrets 的权限。
OBS 项目需预先设置 `windows_x64/x86_64` 仓库；参考现有 `home:Leetfs:VRCS`，
使用 `openSUSE:Factory/snapshot` 和 `windows:mingw:win64/openSUSE_Tumbleweed`。

```sh
npm install --prefix .release/signing-cli --ignore-scripts --no-audit @tauri-apps/cli@2.11.4
python3 scripts/release/setup_release.py --repo OWNER/VRCS --obs-project home:OBS_USER:VRCS
```

脚本交互读取 OBS 凭据，创建加密的 Tauri updater 密钥，并通过 stdin 设置 GitHub Secrets：
`OBS_USERNAME`、`OBS_PASSWORD`、`TAURI_SIGNING_PRIVATE_KEY`、
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`、`TAURI_UPDATER_PUBLIC_KEY`。
设置变量 `OBS_PROJECT`、`OBS_PACKAGE`（默认 `vrcs-windows-actions`）、
`OBS_REPOSITORY`（默认 `windows_x64`）。备份目录默认 `.release/signing/`，目录 0700、文件 0600，已被 git 忽略。
已有 Tauri Secrets 时脚本拒绝生成/覆盖密钥，OBS 凭据应单独配置。
迁移到上游必须保留其已有更新签名密钥；客户端内的公钥不能随意更换。
仓库地址、源码提交、更新端点由当前 `GITHUB_REPOSITORY` 生成，无需改写 Leetfs 地址。
如默认分支不是 main，调整工作流的 push branches。

## 源码及工具输入

实际提交到 OBS 的 `VRCS-VERSION.tar.xz` 来自版本 commit 的 `git archive`，不使用上游 VRCS EXE。
`input-cache.json` 指向成功构建修订 25 的固定第三方工具/运行库归档；
复制时校验文件名，OBS 解包前按 `SHA256SUMS.inputs` 验证 SHA-256。
Cargo/npm 锁文件的外部依赖变化会重新生成 vendor/cache；单纯版本递增可复用依赖归档。
如升级 Rust/CUDA/Tauri/运行库，在新的 OBS 基线中准备归档后同步 pins 和缓存指纹。
`prepare_dependencies.py` 保存官方依赖归档准备函数；工作流只在锁文件变化时调用需要的函数。

### 固定归档的实际来源与传输链路

| OBS 文件 | 官方来源及准备方式 |
| --- | --- |
| rustc/cargo/rust-std 1.99.0 `.tar.xz` | `static.rust-lang.org/dist` 官方发行包，按 Rust channel manifest 中的 hash 校验；原包上传 |
| cuda-cross-13.2.0.tar.xz | NVIDIA 官方 CUDA redist manifest 指向的 Linux device tools、Windows headers/import libraries，与 NVIDIA CCCL 2.8.2 源码合并；移除 runtime DLL，生成 GNU import 所需 exports，重新打包 |
| VulkanRT-1.4.309.0-Components.zip | LunarG `sdk.lunarg.com` 官方 Windows Vulkan loader；原包上传 |
| vulkan-headers-1.4.309.tar.gz | KhronosGroup/Vulkan-Headers 的 v1.4.309 源码；原包上传 |
| onnxruntime-win-x64-1.24.4-runtime.tar.xz | microsoft/onnxruntime 官方 Windows 1.24.4 zip，去掉 PDB 后重新打包；运行库为第三方预编译 DLL |
| tauri-tools-2.11.4.tar.xz | npm 官方 Tauri CLI JS/Linux binding、tauri-apps 官方 NSIS 3.11 stubs/includes/plugins、nsis_tauri_utils 0.5.3，以及微软 WebView2 bootstrapper；合并打包 |
| nsis-v311.tar.gz | kichik/nsis 官方 v311 源码；Linux makensis 在 OBS 内编译，Windows installer stubs/plugins 来自上述官方二进制包 |

这些固定归档最初由本机准备脚本从官方 HTTPS 地址下载并上传到 OBS，
成功的源修订 25 保留全部输入。Rust/NVIDIA 原始组件按官方 manifest 校验；其他下载项固定 SHA-256。
自己合并/裁剪的归档，其整体 SHA-256 由准备过程生成并维护，不是厂商提供的聚合包。

当前 Actions 首先执行 OBS 服务端 `cmd=copy`，来源为
`home:Leetfs:VRCS/vrcs-windows@25`，目标为变量指定的发布包。
OBS 在服务端复制已有源文件，因此 GitHub runner 不重复下载/上传这些固定大包。
随后 Actions 上传本次源码、spec、脚本、公开更新配置和 SHA256SUMS.inputs，
用 commitfilelist 提交最终文件集合，移除旧版本源码及不用的基线文件。
最后分别显式请求 `包名:standard`、`包名:cuda` 的 rebuild，并核对 srcmd5 对应的构建日志。

VRCS/Whisper/前端在 OBS 源码构建；Rust/CUDA 编译工具及 ONNX/Vulkan runtime、
NSIS stubs/plugins 属于固定第三方预编译输入。当前工作流需要上述已有 OBS 基线。

OBS 不接收私钥。它只嵌入公钥及本仓库 HTTPS 更新地址，使用 Linux LLVM/MinGW 交叉编译。
GitHub 下载后验证版本/源码提交/公钥/校验和，以 Tauri CLI 签名，再用独立 minisign 验证。
`latest.json` 分别提供 `windows-x86_64-standard` 和 `windows-x86_64-cuda`，客户端按功能选择目标。
发布先建 draft、上传完整 EXE/.sig/.sha256/latest.json/源码及 provenance，最后才公开为 latest。
Tauri 更新签名不等于 Windows Authenticode。以前禁用更新的 OBS 0.2.1 客户端需要手动安装首个签名版。

## 保留的检查

- OBS：已有前端单元测试、i18n、最终 x64 PE、每个 CUDA fatbinary 的五架构及 PTX 检查。
- Actions：版本/锁文件同步、依赖缓存失效、作者/commit 记录的少量测试。
- 签名：发布前验证密钥匹配、拒绝被修改的字节，并验证两版最终 EXE 的签名。
- 上游原有 Rust/前端测试代码及 CI 保留；Linux OBS 无法执行 Windows/GPU 实机测试。

移除了调试阶段的 CUDA probe、NSIS probe 及单独的 smoke 包；仅构建最终应用与安装器。
NSIS 3.11、Tauri CLI 2.11.4、上游模板/hooks、嵌入 WebView2 引导程序保持对齐。
CUDA 为 75/80/86/89/120a cubin 与 PTX 89。Cargo/CMake/NSIS 编译使用 `nproc` 全部核心。

## 手动重跑与诊断

```sh
gh workflow run release.yml --repo OWNER/VRCS
gh run list --repo OWNER/VRCS --workflow release.yml
```

失败保留 Actions artifact 中的 OBS build logs、源修订及打包配置。
OBS 包常驻以便后续重建；项目配置和之前手动构建的包不会被覆盖。
发布前提交版本失败时不会 force-push，避免覆盖构建期间到达的新提交。
GitHub 分支保护如果阻止 bot 版本提交，应配置允许发布 bot 写入或改为版本 PR 流程。
