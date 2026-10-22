> 仅供个人学习与逆向研究使用。请尊重原作者版权与平台规则，勿用于侵权或商业用途。


> ⚠️ **上游 v1.2.3 及更早版本包含一个静默落盘的采集器**：它在 App 进程内 hook okhttp，把搜索结果、笔记详情、**评论区**、用户简介等接口的响应体写入 `/data/data/com.xingin.xhs/files/xhs-harvest/notes.ndjson`，并会自行发起子评论翻页请求（实测：用户只翻了 3 次评论，它自己请求了 2317 次）。仓库里还附带一整套 PC 侧爬取/飞书推送工具链。
>
> 该采集器**已在本仓库中完整移除**（代码 + 工具链 + 文档）。如果你用的是上游旧版：
> - 关闭采集：`touch /data/data/com.xingin.xhs/files/xhs-harvest/OFF`
> - 清除已采集数据：`rm -f /data/data/com.xingin.xhs/files/xhs-harvest/{notes.ndjson,expanded_roots.txt,urls.log}`

---

## 自行构建

需要 JDK 11+、Android SDK（build-tools + 一个 platform 的 `android.jar`）。

```bash
ANDROID_HOME=~/Android/Sdk ./build.sh
# 产物: xhs-savefix.apk
```

纯 Java，无 native 依赖。核心代码就一个文件：[`app/src/main/java/com/chekayo/xhssavefix/XhsSaveFix.java`](app/src/main/java/com/chekayo/xhssavefix/XhsSaveFix.java)。

---

## 免责声明

本项目为逆向学习产物，仅在使用者自有设备上解除客户端 UI 限制，不修改、不访问小红书服务器，不涉及账号或数据破解。是否保存、如何使用所保存的内容由使用者自行承担法律与道德责任，作者不对任何滥用负责。

---

## License

[MIT](LICENSE)