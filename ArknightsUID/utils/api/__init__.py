"""ArknightsUID API 层。

- `smsdk`: 森空岛 dId 设备指纹生成（复用自 EndUID）
- `request_util`: RespCode 业务码 + UA/vName 常量 + 登录链路请求头构造
  （请求签名不在本层：见 utils/skland/client._generate_sign）
"""
