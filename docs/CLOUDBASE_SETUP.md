# CloudBase 云同步启用说明

当前程序固定连接以下测试环境：

- 环境 ID：`production-schedule-test-d73e723`
- 地域：上海（`ap-shanghai`）
- 云存储模式：传统模式
- Bucket ID：`7072-production-schedule-test-d73e723-1460691865`
- 登录方式：CloudBase 用户名、邮箱或手机号 + 密码

## 第一次使用

1. 在 CloudBase 控制台保持“用户名密码登录”开启。
2. 云存储保持私有，不要改成公开读写。
3. 如控制台启用了 Web 发布密钥校验，请创建 **Publishable Key**，在程序登录页的“连接设置”中填写。Publishable Key 可以放在客户端；不要填写或提供 `SecretId`、`SecretKey`。
4. 3.5.3 及以上桌面版固定通过 `localhost:61375` 访问本机服务，
   使用 CloudBase 默认的 `localhost` 安全来源，不需要购买或添加
   自定义域名。
5. 使用测试账户登录。首次登录会生成 `PLS1.` 开头的恢复密钥，请保存到公司的密码管理器。

程序 3.5.2 及以上版本已内置上述存储模式和 Bucket ID，不需要手工填写，
并会把首次同步时的 `Storage file not exists.` 识别为正常的空云端状态。
若以后更换 CloudBase 环境，可在登录页“连接设置”或登录后的同步中心修改
Bucket ID，并通过 `CLOUDBASE_STORAGE_MODE` 指定 `classic`、`pg` 或 `auto`。

如果启动时提示旧的本机安全登录信息读取超时，直接用原测试账号重新登录即可；
这不会删除本机数据库或历史排班。

如果同步中心提示云存储上传失败，依次检查：

1. 软件显示的本地来源为 `localhost:61375`；
2. “身份认证 → 权限控制”的 `StoragesHttpApiAllow` 允许登录用户访问；
3. 云存储“权限设置”的安全规则允许当前登录用户读写；
4. 保存控制台设置后等待约 3 分钟，再回到程序点击“立即同步”。

## 云存储安全规则

建议为同步目录使用“仅创建者可读写”的规则。CloudBase 控制台的规则语法更新时，以控制台校验结果为准：

```json
{
  "read": "resource.openid == auth.openid || resource.openid == auth.uid",
  "write": "resource.openid == auth.openid || resource.openid == auth.uid"
}
```

不要使用所有用户可读或匿名可读规则。程序上传的是 AES-256-GCM 加密数据库快照，但私有访问控制仍然需要保留。

## 第二台设备

1. Windows 设备解压 `产线排班系统-Windows-x64.zip`。
2. 双击文件夹内的 `产线排班系统.exe`，不需要安装 Python 或 Node.js。
3. 使用同一个 CloudBase 账户登录。
4. 首次登录此设备时输入第一台设备保存的恢复密钥。
5. 程序会下载并恢复最新云端排班；后续修改会自动同步。

## 冲突与备份

- 两台设备同时离线修改时，程序不会静默覆盖，而是要求选择“使用云端最新数据”或“保留本机并上传”。
- 恢复云端数据前，本机数据库会自动备份到应用数据目录的 `backups` 文件夹，最多保留最近十份。
- Mac 的恢复密钥保存在系统钥匙串；Windows 保存在 Windows 凭据管理器。
- 退出账户会删除本机保存的刷新令牌，但不会删除业务数据库或云端历史版本。
