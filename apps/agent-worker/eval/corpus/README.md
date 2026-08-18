# Corpus

- `documents/`：fetch runner 下载的授权明确公开 PDF，本地存在但不进入普通 Git。
- `private/`：本地私有或失败回流样本，Git 默认忽略其正文。
- `generated/`：由仓库脚本确定性生成的 PDF。

所有正文都必须由 case manifest 引用并记录 SHA-256。不要在这里保存许可不明、未脱敏或含敏感信息的
文件。公开文件通过固定 URL 重建；大文件长期共享应使用受控对象存储或 Git LFS。
