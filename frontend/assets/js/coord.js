/* ─────────────────────────────────────────────────────────────
 * coord.js  —  WGS-84 / GCJ-02 互转（纯前端，无依赖）
 *
 * 站点源数据 (站点经纬度坐标.csv / 国家水质自动监测网) 为 WGS-84；
 * 高德底图 (webst0x.is.autonavi.com/appmaptile) 为 GCJ-02 火星坐标。
 * 直接叠加会产生 100~600m 偏移，因此所有站点经纬度在贴图前必须纠偏。
 * ───────────────────────────────────────────────────────────── */
(function (global) {
  const PI = Math.PI;
  const A = 6378245.0;                 // 克拉索夫斯基椭球长半轴
  const EE = 0.00669342162296594323;   // 偏心率平方

  // 中国境外不做偏移
  function outOfChina(lng, lat) {
    return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55);
  }

  function transformLat(x, y) {
    let r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * PI) + 320 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
    return r;
  }

  function transformLng(x, y) {
    let r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
    return r;
  }

  /** WGS-84 → GCJ-02（站点坐标贴到高德底图时使用） */
  function wgs2gcj(lng, lat) {
    if (outOfChina(lng, lat)) return { lng: lng, lat: lat };
    let dLat = transformLat(lng - 105.0, lat - 35.0);
    let dLng = transformLng(lng - 105.0, lat - 35.0);
    const radLat = lat / 180.0 * PI;
    let magic = Math.sin(radLat);
    magic = 1 - EE * magic * magic;
    const sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sqrtMagic) * PI);
    dLng = (dLng * 180.0) / (A / sqrtMagic * Math.cos(radLat) * PI);
    return { lng: lng + dLng, lat: lat + dLat };
  }

  /** GCJ-02 → WGS-84（地图点击取点、回写源数据时使用，一次迭代足够 1m 级精度） */
  function gcj2wgs(lng, lat) {
    const g = wgs2gcj(lng, lat);
    return { lng: lng * 2 - g.lng, lat: lat * 2 - g.lat };
  }

  global.GCJ = { wgs2gcj: wgs2gcj, gcj2wgs: gcj2wgs, outOfChina: outOfChina };
})(window);
