# Docker 部署指南

适用于已有 HTTPS 反向代理的 Linux 服务器，包括 OCI ARM64 实例。
部署模板限制应用使用 1 GiB 内存和 1 个 CPU，不向宿主机发布应用端口。

## 准备配置

1. 安装 Docker 和 Compose 插件，确认反向代理已有可用的 Docker 网络。
2. 将 `deploy/compose.env.example` 复制到服务器的 `/etc/forkiq/compose.env`，
   设置权限为 `600`，填写实际公网 HTTPS 地址、代理网络名和代理容器 IP。
   示例中的域名和 IP 仅为占位值，不能直接用于生产。
3. 创建 `/var/lib/forkiq`，所有者设为容器使用的 UID/GID `10001:10001`，
   目录权限设为 `700`。数据库、App 私钥及仓库备份保存在此目录。
4. 将代码放到 `/opt/forkiq`，运行：

```sh
cd /opt/forkiq
sudo docker compose --env-file /etc/forkiq/compose.env \
  -p forkiq -f deploy/compose.oci.yml up -d --build
```

反向代理必须与应用连接同一个网络，回源地址为 `forkiq:8792`。
把域名解析到服务器并配置有效的 HTTPS 证书。使用 Cloudflare 代理时，
源站应具备有效证书，SSL/TLS 使用 Full (strict)。证书验证路径需要能正常回源。
添加站点前先保存反向代理的当前配置，验证后只重载必要的变更。

首次启动后，在开放公网访问之前通过受控通道完成管理员初始化。
之后创建或连接 GitHub App，按需授权仓库。启用同步前核对每个仓库的同步模式和保护设置。

## 运维与升级

```sh
sudo docker compose --env-file /etc/forkiq/compose.env \
  -p forkiq -f /opt/forkiq/deploy/compose.oci.yml ps
sudo docker logs --tail 100 forkiq-forkiq-1
```

`FORKIQ_PROXY_IP` 必须与实际反向代理容器 IP 一致；代理重建导致 IP 变化时，
更新服务器配置并重新创建应用容器。不要把真实配置、密码、私钥、数据库、
备份文件或包含服务器细节的运维记录提交到 Git。

升级前暂停同步并保全数据库、App 私钥及备份目录，再更新源代码、构建镜像，
验证健康状态和仓库保护设置后按需恢复同步。Docker 部署不使用面向
systemd 安装的 `deploy/update.sh`。实际事故记录应存放于代码仓库之外。
