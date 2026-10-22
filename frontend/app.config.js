/* ══════════════════════════════════════════════════════════════════════════
 * app.config.js — 部署配置
 *
 * 【前后端分离时，这是唯一需要改的文件】
 * 前端是纯静态应用：把整个 frontend/ 目录放到任何静态服务器上
 * （nginx / IIS / 对象存储 / 内网共享），再把 apiBase 指到后端地址即可。
 * 后端侧记得把本页来源加进 WQ_CORS_ORIGINS 白名单。
 * ══════════════════════════════════════════════════════════════════════════ */
window.WQ_CONFIG = {
  /* 后端 API 根地址。
   *   ''                    同源（前端由后端顺带托管时用这个，默认）
   *   'http://10.0.0.5:5000' 跨源部署（后端需放行本页来源）
   */
  apiBase: '',

  /* 外观：'dark' 适合值班大屏，'light' 适合汇报与打印。用户可在界面里切换。 */
  theme: 'dark',

  /* 地图底图。留空或全部加载失败时退化为内置纯色底图 —— 完全离线仍可用。 */
  basemaps: {
    vector: {
      label: '矢量路网',
      url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
      subdomains: '1234',
      maxZoom: 18,
      attribution: '高德地图'
    },
    image: {
      label: '卫星影像',
      /* 固定 webst01 单子域。原地址是 webst0{s} + subdomains 1~4 轮转，
       * 按反馈那个地址不好用（各子域影像不一致 / 加载不稳）。
       * 这里不再含 {s}，所以下方也不再需要 subdomains。 */
      url: 'https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
      maxZoom: 18,
      attribution: '高德影像'
    }
  },
  defaultBasemap: 'vector',

  /* 初始视图：湘江流域中下游 */

  /* 每个断面拉取的历史条数（属性表与图表共用） */
  historyLimit: 120,

  /* 界面标题（部署给不同业主时可改） */
  appTitle: '湘江流域水质智能预警平台',
  appSubtitle: '总氮（TN）实时预测 · 断面监测'
};
