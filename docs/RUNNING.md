# 运行与测试

推荐 Windows + Python 3.12；依赖固定在 requirements.lock。首次安装需联网；模型下载按固定 revision 和 SHA256 校验，不随上游 main 变化。其他平台尚未独立验证。

## 数据边界

数据目录必须在源码仓库之外，必须是新目录。setup_demo 拒绝覆盖已有目录，沿用服务端数据库路径保护。不要把项目用于迁移自己的旧库；公开演示流程只创建空的合成数据库和随机账号。

初始化不启动服务。demo_launch 验证数据标记、配置和端口后启动，worker 仅处理该合成库。默认所有演示账号共用一个本次随机生成的密码，只为本机演示，不是生产账号配置。

安装后按根 README 初始化和启动，用界面导入 examples/backup-guide.md。导入同 key、新 version_label 可创建版本。检查任务完成后再搜索；停机后用原配置重启。若希望保留备份，只在停止服务后复制自己的整个外部数据目录，注意其中包含凭据；不要上传。

## 后端测试

在仓库根目录使用已安装依赖的 Python：

```powershell
.\.venv\Scripts\python.exe -B -m tools.run_b2_validation all --evidence-root ..\techdocs-test-results --run-id backend-01
```

运行器使用独立合成数据，包含真实 HTTP 进程重启；失败即停止并保留结果。每次换新的 run-id，不覆盖旧证据。测试需要 Git 可执行文件。模型计算在后端单元测试中使用可控替身；真实模型由浏览器流程另验。

## 浏览器测试

需要 Node.js、Playwright 包和已安装的 Microsoft Edge。安装测试依赖会联网，例如 `npm install --no-save playwright`。没有 Node/Edge 时不要把这部分标成通过。

```powershell
$env:TECHDOCS_MODEL_DIR = (Resolve-Path .\models).Path
.\.venv\Scripts\python.exe -B tools/run_ui_acceptance.py --private ..\techdocs-ui-main --evidence ..\techdocs-ui-main-results --script e2e.cjs
.\.venv\Scripts\python.exe -B tools/run_ui_acceptance.py --private ..\techdocs-ui-extra --evidence ..\techdocs-ui-extra-results --script supplement.cjs
.\.venv\Scripts\python.exe -B tools/run_ui_acceptance.py --private ..\techdocs-ui-polish --evidence ..\techdocs-ui-polish-results --script ui-polish.cjs
```

所有目录必须尚未存在，端口 8388 必须空闲。runner 自动将当前 Python 解释器传给测试；Playwright 默认从 node_modules 加载，也可设 PLAYWRIGHT_MODULE 为已有模块绝对路径。不修改产品权限检查、不替换真实翻译或密码依赖。

## 未验证入口

Dockerfile、compose.yaml 与 container_check 保留用于健康检查契约测试；没有本公开版的实际 Docker/Linux 验收，不推荐据此直接上线。没有启用 GitHub Actions，不会因本次推送自动启动远端测试。
