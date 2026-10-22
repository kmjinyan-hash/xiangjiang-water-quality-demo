#!/bin/sh
# ─────────────────────────────────────────────────────────────
# 启动脚本：把镜像内置的"参考数据"同步到 /app/data/geo
#
# 为什么需要它：
#   /app/data 通常被命名卷挂载（数据库与数据 CSV 要持久化），而**卷会整体覆盖**
#   该目录，把镜像里烘焙的内容遮住。参考数据（流域边界、土地利用瓦片）如果直接
#   放在 /app/data/geo，就会被卷遮掉 —— 容器就不自包含了，迁移时必须额外带
#   宿主机目录并挂载，容易漏。
#
# 同步策略：比对"文件数 + 占用空间"作为版本指纹。
#   · 首次启动：把 /app/geodata 拷进 /app/data/geo；
#   · 镜像更新（瓦片重生成、边界替换）：指纹变化，覆盖成新版本；
#   · 未变化：跳过，避免每次启动都遍历几千个瓦片。
#   以镜像为准是有意的：参考数据是随版本发布的静态资产，不是运行期状态；
#   运行期状态（app.db、断面 CSV）仍在卷里正常保留。
#
# 兜底：若 /app/data/geo 不可写（例如卷里遗留 root 属主的同名目录），
#   直接把 WQ_GEO_DIR 指到镜像内副本，功能不受影响。
# ─────────────────────────────────────────────────────────────
set -e

GEO_SRC=/app/geodata
GEO_DST=/app/data/geo
GEO_VER=$GEO_DST/.geo-version

geo_fingerprint() {
    # 文件数 + 占用空间(KB) + 最新修改时间。
    # 只数文件数和体积不够：重新生成同尺寸的瓦片时两者都不变（实测踩过），
    # 导致镜像更新不同步。加上最新 mtime 才能真正识别内容变化。
    _n=$(find "$1" -type f 2>/dev/null | wc -l)
    _k=$(du -sk "$1" 2>/dev/null | cut -f1)
    _t=$(find "$1" -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1)
    echo "${_n}-${_k}-${_t}"
}

if [ -d "$GEO_SRC" ]; then
    SRC_VER=$(geo_fingerprint "$GEO_SRC")
    DST_VER=$(cat "$GEO_VER" 2>/dev/null || echo "none")

    if [ "$SRC_VER" != "$DST_VER" ]; then
        if mkdir -p "$GEO_DST" 2>/dev/null && [ -w "$GEO_DST" ]; then
            # 覆盖：参考数据以镜像为准
            cp -rf "$GEO_SRC/." "$GEO_DST/" 2>/dev/null || true
            echo "$SRC_VER" > "$GEO_VER" 2>/dev/null || true
            echo "[entrypoint] 参考数据已同步到 $GEO_DST（版本 $SRC_VER）"
        fi
    fi

    if [ ! -f "$GEO_DST/basin.geojson" ] || [ ! -d "$GEO_DST/tiles" ]; then
        export WQ_GEO_DIR="$GEO_SRC"
        echo "[entrypoint] $GEO_DST 不可用，改为直接使用镜像内置的 $GEO_SRC"
    fi
fi

exec "$@"
