# Use AI Every Server

在使用老旧或特殊架构的远程服务器时，我们常常无法直接在服务器上运行现代的 AI-Coding 插件（例如 Trae, Cursor 等）或遇到了各种环境依赖的冲突。

**Use AI Every Server** 是一个轻量级的 Python 脚本工具，它可以让你在本地环境愉快地使用 AI 辅助编程，同时将指定的代码自动同步到远程服务器并立即执行，随后在本地终端流式输出服务器上的运行日志与错误信息。

这实现了一种“本地编写/AI辅助，远程运行/调试”的平滑工作流。

## 🌟 核心特性

- **按需同步**：只上传你指定需要修改和执行的脚本/代码文件，速度极快。
- **环境隔离配置**：支持分离的环境变量加载（如 `conda`、自定义的 `LD_LIBRARY_PATH` 等），自动兼容复杂的 Linux 训练/运行环境。
- **支持 Screen 会话（新增！）**：针对需要通过 `screen` 申请计算节点（如 GPU）的集群环境，支持将命令自动发送到指定的 `screen` 会话中执行。
- **安全的凭据管理**：利用 `.env` 环境变量存储密码，不会不小心将密码提交到 Git 仓库中。
- **实时日志流式输出**：像在本地执行一样，实时查看远端服务器上执行程序的 `stdout` 和 `stderr` 输出（如果使用 Screen 模式，则需要进入服务器对应 screen 查看）。

## 📦 快速开始

### 1. 安装依赖

请确保你的本地环境已经安装了 Python 3。然后安装所需的包：

```bash
pip install -r requirements.txt
```

### 2. 配置服务器信息

复制环境模板并填入你的服务器凭据：

```bash
cp .env.example .env
```
编辑 `.env` 文件：
```env
REMOTE_HOST=192.168.1.100
REMOTE_PORT=22
REMOTE_USER=your_username
REMOTE_PASS=your_password
```

### 3. 配置同步规则与运行命令

复制配置模板：

```bash
cp config.example.yaml config.yaml
```

打开 `config.yaml`，按需修改：

```yaml
sync:
  local_root: "." # 本地项目根目录
  remote_root: "/data/project" # 远程服务器的项目根目录
  files_to_sync:
    - "src/main.py"
    - "run.sh"

run:
  # (可选) 如果你需要在指定的 screen 会话里运行命令（比如申请了 GPU 节点的 screen）
  # 请填写 screen 会话的名称。留空则直接在当前 SSH 环境运行。
  screen_session: "my_gpu_session" 
  
  env_setup: "export PATH=/path/to/conda/bin:$PATH" # 运行前的环境配置
  command: "bash run.sh" # 具体要执行的命令
```

### 4. 运行工具

准备好以后，在本地修改完代码后，直接运行：

```bash
python main.py
```
*(你也可以通过 `python main.py -c other_config.yaml` 来指定其他配置文件)*

## 🛠️ 使用场景建议

1. 将 `use-ai-everyserver` 工具放在你正在开发的项目目录下。
2. 配置好 `config.yaml` 的同步列表，包含你通常频繁修改的核心逻辑脚本或 `bash` 执行脚本。
3. 利用 AI 工具完成代码修改后，在本地终端敲下 `python main.py` 即可在远端自动运行并查看到结果，省去手动拖拽文件、打开 SSH 的麻烦！

## 📄 许可协议

MIT License
