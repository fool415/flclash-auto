# 第三方组件声明 (Third-Party Notices)

本仓库 `flclash-auto` 本身以 **MIT 许可证** 发布，见 [LICENSE](./LICENSE)。

## FlClash

本工具是针对 [FlClash](https://github.com/chen08209/FlClash) 的第三方命令行调度器：
它在需要时启动 FlClash 客户端、通过其本地 Clash API 测速并切换节点，命令结束后关闭它。
本工具**不包含、不修改 FlClash 的任何源码**，仅作为独立进程启动其可执行文件并调用其本地 HTTP API（`127.0.0.1:9090`）。

FlClash 是 [chen08209](https://github.com/chen08209) 开发的开源软件，基于 Clash Meta（mihomo），
以 **GNU General Public License v3.0（GPL-3.0）** 发布：

- 项目仓库：<https://github.com/chen08209/FlClash>
- 官方发布下载：<https://github.com/chen08209/FlClash/releases>
- GPL-3.0 许可证全文：见本仓库 [LICENSE-GPL-3.0.txt](./LICENSE-GPL-3.0.txt)

## 再分发说明

本仓库的 GitHub Release 会附带 FlClash **官方构建** 的 Windows 便携版（portable `.zip`）资产，
方便用户一步到位地使用本工具。该资产：

- 直接取自 FlClash 官方 [Releases](https://github.com/chen08209/FlClash/releases)，**未做任何修改**；
- 版权归 FlClash 项目原作者所有，遵循 GPL-3.0 许可证；
- 对应完整源码可在上述官方仓库获取。

FlClash 是 Clash Meta（mihomo）的衍生作品，mihomo 同样遵循 GPL-3.0 许可证。
