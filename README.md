# ASX 做空 & 成交量追踪（App + 后端）
后端：GitHub Actions 每个工作日悉尼收盘后运行 fetch_data.py，抓 ASIC 全部做空历史 + 行情成交量，计算对比与买卖信号，写入 data/data.json。
前端：index.html 是 PWA，读取 data/data.json。

## 5 步部署（免费，约10分钟）
1. 注册 github.com，新建仓库（Public），把本文件夹所有文件上传（含隐藏的 .github 文件夹）。
2. 仓库 Settings → Pages → Source 选 "Deploy from branch"，分支 main，目录 /(root)。
3. 仓库 Actions 页 → daily-update → Run workflow，手动跑一次（首次回填约250个交易日历史，需几分钟）。
4. 打开 https://你的用户名.github.io/仓库名/
5. 手机添加到主屏幕：iPhone 用 Safari → 分享 → 添加到主屏幕；安卓用 Chrome → 菜单 → 安装应用。

## 注意
- ASIC 数据约滞后4个交易日；行情来自 Yahoo Finance（非官方，可能延迟或限流）。
- ASX 公告接口为非官方，失败时自动跳过。
- 通知：打开 App 且有新信号时本地提醒；真正后台推送需另加推送服务。
- 首次运行如 ASIC 文件格式有变，请把 Actions 日志发给我，我来改。
