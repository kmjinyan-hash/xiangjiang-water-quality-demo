> 历史技术文档：本文可能包含旧版登录、内网部署或训练指令。公开版本不提供训练源代码；启动和授权范围以仓库根 README、LICENSE 为准。

# APK 打包路径（预留，尚未执行）

当前状态：**先不打包，但路已留好**。这份文档记录打包需要什么、现在卡在哪、按什么顺序做。

## 一、为什么现在打不了

| 依赖 | 现状 | 打包需要 |
|---|---|---|
| 认证方式 | cookie（HttpOnly） | token（WebView 里 cookie 必被拦） |
| 前端产物 | `dist/` 由 Caddy 或本机服务器提供 | 必须**纯静态**，任何托管都能发 |
| 后端可达性 | `127.0.0.1:8443`（仅本机） | 手机要能访问 → 局域网或公网 |
| 证书 | Caddy 自签 | Android 默认拒绝自签 |

## 二、已经留好的口子

### 1. 后端支持可选的 Bearer token

```
POST /api/auth/login
X-Auth-Mode: bearer          ← 带上这个头，响应体才额外返回 token

→ 200 {"data":{"username":"...","role":"...","token":"...","auth_mode":"bearer"}}
```

不带这个头时**行为完全不变**：只下发 HttpOnly cookie，响应体没有 token。
同源前端读不到凭证，安全性不受影响。

之后请求带：

```
Authorization: Bearer <token>
```

`auth.token_from_request()` 早已支持这个头，服务端校验同一条会话记录，
退出即失效（删库里的行），不是自包含签名。

> **约束**：打包客户端只能把 token 放在**内存变量**里，不写 localStorage、
> 不写文件。刷新/重开 = 重新登录。这是为了不在客户端持久化任何凭证。

### 2. 前端是纯静态产物

`dist/` 只有 HTML/CSS/JS，没有任何服务端依赖。用 `--api-base` 指定后端地址即可：

```bash
python scripts/build_frontend.py --api-base https://<后端地址>:8443
```

产物里的 `app.config.js` 会变成 `apiBase: 'https://...:8443'`。

### 3. CORS 按来源放行

后端已实现"回显 Origin + Allow-Credentials"，用 `WQ_CORS_ORIGINS` 配置白名单。
Capacitor 的 origin 需要加进去：

```
WQ_CORS_ORIGINS=http://localhost,capacitor://localhost,https://localhost
```

## 三、打包步骤（等真要打时按此执行）

### 1. 前端改成内存 token

在 `frontend/assets/js/core.js` 里：

```js
var TOKEN = null;                    // 内存，不持久化

// 登录时
fetch(API_BASE + '/api/auth/login', {
  method: 'POST',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json', 'X-Auth-Mode': 'bearer' },
  body: JSON.stringify({ username, password })
}).then(function (r) { return r.json(); })
  .then(function (j) { TOKEN = j.data.token; });   // ← 只进内存

// 每个请求
if (TOKEN) { opt.headers['Authorization'] = 'Bearer ' + TOKEN; }
```

同时把 `authMode` 做成构建期开关（cookie / bearer 两种产物），
避免同源部署时也被迫用 token。

### 2. 建 Capacitor 工程

```bash
npm install -g @capacitor/cli
npx cap init "水质预警" "com.example.wq" --web-dir=dist
npx cap add android
npx cap sync
npx cap open android        # 用 Android Studio 出 APK
```

### 3. 解决证书

三条路，按情况选：

| 做法 | 说明 |
|---|---|
| 把 Caddy 根证书装到手机 | 内部测试最快：`docker cp wq_web:/data/caddy/pki/authorities/local/root.crt .`，再在 Android「设置 → 安全 → 安装证书」 |
| 域名 + Let's Encrypt | 最干净，需要域名与公网 80/443 |
| 允许明文 | **不推荐**：密码明文过网络 |

### 4. 开放后端网络

现在 Caddy 只绑 `127.0.0.1`，手机连不上。打包前必须改：

```bash
# 局域网
docker run ... -p 192.169.30.157:8443:8443 caddy:alpine
```

这一步会**把服务暴露到局域网**，与"不暴露接口"的要求冲突，需明确同意后再做。

## 四、现在怎么用（不受影响）

```bash
python scripts/serve_frontend.py     # 主应用，浏览器打开 http://localhost:8080
docker start wq wq_web              # 后端 + 管理台 https://localhost:8443/admin/
```

同源 cookie 认证，一切照旧。
