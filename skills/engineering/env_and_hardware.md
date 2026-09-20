# 机器与运行时环境使用指南 (Machine & Environment Guide)

本文档记录当前运行节点的核心机器约束与环境事实。遇到显卡状态、网络下载、磁盘空间及生命周期疑问时自主查阅。

---

## 1. GPU 显卡与进程管理

- **显存与利用率假象**：
  - 本机存在常驻显存的占卡守护进程（随时会在任务提交/启动时自动让位退出）。
  - **不要看** `nvidia-smi` 顶部的 `Memory-Usage` 和 `GPU-Util` 数字。**严禁**因为看到显存占用高或利用率非零就误判为"卡忙"而等待、放弃或缩水实验规模。
- **目标卡空闲判断标准**：
  - **只看** `nvidia-smi` 最下方 `Processes` 进程表：目标卡上没有其他活跃的用户大任务进程 = 当前空闲，可以直接启动实验。
- **可见卡限制**：
  - 默认遵循系统分配的 `CUDA_VISIBLE_DEVICES`，独占目标卡。
- **僵尸进程自检**：
  - 遇到显存 OOM 或端口冲突时，先检查并清理自己之前卡死或未退出的 Python / vLLM / torchrun 孤儿进程。

---

## 2. 网络代理与数据集/权重下载 (`hf_download`)

- **网络代理配置**：
  - 访问外网必须走代理 `http://agent.baidu.com:8188`（HTTP/HTTPS 同址）。
  - **禁止修改** `HF_ENDPOINT` 到 `hf-mirror.com`（本机访问 mirror 会超时，必须直连 `https://huggingface.co`）。
- **单连接断流问题与多连接下载**：
  - 代理对**单连接**限速 ~1.4MB/s 且在恰好 **10MiB（10485760 字节）处掐断**。
  - **严禁**直接用单连接 Python（如 `load_dataset("org/repo")`、`requests`）直接拉取 >50MB 的外部大文件。
  - **多连接下载工具**：拉取 >50MB 的数据集或模型，必须使用同目录提供的多连接脚本：
    ```bash
    bash skills/hf-dl.sh <org/repo> --dataset      # 下载数据集
    bash skills/hf-dl.sh <org/repo>                # 下载模型
    ```
  - 下载完成后按本地 parquet 或本地路径加载（详见 `skills/hf-download.md`）。

---

## 3. 存储与磁盘空间 (GPFS)

- **严禁全盘 `find` / 从根起步的递归扫描**：
  - `/root/paddlejob/rl-public` 是共享 GPFS 集群盘（600T、常年 98%+）。`find /`、`find /root`、`find /root/paddlejob/rl-public …` 这类从根或挂载点起步的遍历**永远扫不完**，且会卡死在不可中断的 GPFS 等待（D 态 / `cxiWaitEventWait`）——连 SIGKILL 都要等内核调用返回才能回收，加 `2>/dev/null` 只会掩盖它已经挂住。
  - 更坑的是：一次超时的 `find /` 会在后台继续爬盘，并拖住发起它的 agent（后者阻塞在等这个子进程退出），使节点在正事早已完成后仍白白空耗，直到墙钟超时才被兜底。
  - **搜索范围只允许**限定在自己的工作目录内，或最多不超出 `/root/ComateProjects/chats` 与 `/root/paddlejob/rl-public/xiasheng01/` 这两个根，且仍需谨慎：优先用已知绝对路径直接 `ls` / 读取；确需搜索时务必加 `-maxdepth`、指定具体子目录、避免宽泛通配，宁可分几次窄搜也不要一把梭全局。

---

## 4. 时间管理与生命周期

- 每个节点都有最大墙钟时长限制（`NODE_TIMEOUT_MIN`）。
- 节点到达超时上限会被系统**无条件 SIGKILL 强杀**。
- 使用 `bash timer.sh` 可随时查看当前实验 run 的全局剩余时间；所有实验产物、日志与评测报告必须**边做边落盘**。
