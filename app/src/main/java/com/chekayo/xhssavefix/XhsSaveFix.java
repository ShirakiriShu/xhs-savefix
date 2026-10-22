package com.chekayo.xhssavefix;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.List;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

/**
 * 小红书增强模块(com.xingin.xhs):
 *
 * 1) 解除「作者关闭下载/保存」(原 SaveFix 功能),两条门都拆:
 *    - 旧门:笔记数据带 com.xingin.entities.MediaSaveConfig,JSON "disable_save" -> getter b()。
 *      下载路径 DownloadController.onDownloadClick 里 if(b()){toast;return;}。hook 成恒 false。
 *    - 新门(v9.33.x 服务端灰度,issue #2):笔记详情下发权限数组
 *      [{"reason":"作者已关闭下载权限，无法保存","type":"image_download","enable":false},...],
 *      模型 com.xingin.entities.notedetail.FunctionSwitch(enable/reason/type, getter a/b/c)。
 *      长按面板(ap8/e)与分享面板(sv8/u)里「保存图片」按 switch==null||!a() 置灰。
 *      hook a():image_download/video_download 时强制 true。
 *      同结构的旧字段 ShareInfoDetail$Operate(type/.../disable/disableToast) 一并处理。
 *
 * 2) 去水印(保存到相册的图不叠水印):
 *    保存合成器 jm7.j.a(原图, 作者水印, 滤镜, AI标签, disableWaterMark, disableAiWaterMark):
 *      if(!disableWaterMark && 作者水印!=null) 画右下角作者昵称+水印图;
 *      if(!disableAiWaterMark && AI标签!=null) 画左下角「AI生成」标签。
 *    两个开关都是服务端字段:
 *      disableWaterMark  <- MediaSaveConfig.c() (JSON "disable_watermark")
 *      disableAiWaterMark<- ImageBean.u()       (JSON "disable_ai",仅 AIGC 帖有)
 *    注意方向:disable_watermark=true 才是不加水印。旧版模块把 c() 强制成 false,
 *    反而把作者水印强制画上了;现改为恒 true。AI 标签把 ImageBean.u() 恒 true,
 *    另加兜底 hook 混淆类 jm7.j.b()(AI 标签位图构造)返回 null(绑定 9.33.4,失败不致命)。
 *
 * 2.5) 视频保存(第三道门):长按面板通过后,点保存还会请求服务端
 *      GET /api/sns/v10/note/video/save?note_id=X,作者关闭时服务端回
 *      {"data":{"msg":"作者已关闭下载功能，可尝试其他分享方式","disable":true}},无下载地址。
 *      模块平时扫 /api/sns/ 响应缓存 noteId -> 播放用 h264 mp4 直链(仅内存),
 *      拦到该接口 disable:true 时改写为 disable:false 并注入 download_url,让客户端正常落盘。
 *
 * 隐私:本模块不采集、不落盘、不上传任何小红书数据。
 *      (v1.2.3 及更早版本含一个静默落盘采集器,已在 1.3.0 完整移除)
 */
public class XhsSaveFix implements IXposedHookLoadPackage {

    private static final String XHS = "com.xingin.xhs";
    private static final String CFG = "com.xingin.entities.MediaSaveConfig";
    private static final String FUNCTION_SWITCH = "com.xingin.entities.notedetail.FunctionSwitch";

    private static final long MAX_BODY = 512 * 1024; // peekBody 上限,单条响应最多读 512KB

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lpparam) {
        if (!XHS.equals(lpparam.packageName)) return;

        hookSaveFix(lpparam);
        hookNoWatermark(lpparam);
        hookVideoDownloadBranch(lpparam);
        UpdatePrompt.install(lpparam);
        hookVideoSaveApi(lpparam);
    }

    // ---------- 1. SaveFix ----------

    private void hookSaveFix(XC_LoadPackage.LoadPackageParam lpparam) {
        // 旧门:disable_save -> getter b() 恒 false,DownloadController.onDownloadClick 放行。
        try {
            XposedHelpers.findAndHookMethod(CFG, lpparam.classLoader, "b", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    if (Boolean.TRUE.equals(param.getResult())) param.setResult(Boolean.FALSE);
                }
            });
            XposedBridge.log("[xhs-savefix] hooked MediaSaveConfig.b() (disableSaveMedia) -> false");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook b() failed: " + t);
        }

        // 新门(issue #2):服务端权限数组 FunctionSwitch[type/enable/reason]。
        // enable getter a(),type getter c()。仅对图片/视频下载类型强制 enable=true,
        // 不动 edit/privacy 等作者自己的管理开关。
        try {
            XposedHelpers.findAndHookMethod(FUNCTION_SWITCH, lpparam.classLoader, "a",
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        if (Boolean.TRUE.equals(param.getResult())) return;
                        Object type = XposedHelpers.callMethod(param.thisObject, "c");
                        if ("image_download".equals(type) || "video_download".equals(type)) {
                            param.setResult(Boolean.TRUE);
                        }
                    }
                });
            XposedBridge.log("[xhs-savefix] hooked FunctionSwitch.a() (image/video_download enable) -> true");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook FunctionSwitch.a() failed: " + t);
        }

        // 兼容旧字段:shareInfo.function_entries 的 Operate.disable getter a(),
        // type getter g()。e3b/a 会把 disable/disableToast 塞进「保存图片」菜单项。
        try {
            XposedHelpers.findAndHookMethod("com.xingin.entities.ShareInfoDetail$Operate",
                lpparam.classLoader, "a", new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        if (!Boolean.TRUE.equals(param.getResult())) return;
                        Object type = XposedHelpers.callMethod(param.thisObject, "g");
                        if ("image_download".equals(type) || "video_download".equals(type)) {
                            param.setResult(Boolean.FALSE);
                        }
                    }
                });
            XposedBridge.log("[xhs-savefix] hooked ShareInfoDetail$Operate.a() (image/video_download disable) -> false");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook Operate.a() failed: " + t);
        }
    }

    /**
     * 调用 chain.proceed(request),但把 InvocationTargetException 解包成原始异常。
     *
     * 为什么不能用 XposedHelpers.callMethod:它把被调用方法抛出的异常包成
     * XposedHelpers$InvocationTargetError(继承 Error,不是 IOException)。
     * okhttp 的 RealCall 只 catch IOException,Error 会穿透到业务线程变成未捕获异常
     * -> 小红书直接闪退。表现就是网络抖动(Socket is closed / Canceled / timeout)
     * 时概率性闪退,而同样的网络抖动在未装模块时只是加载失败。
     *
     * 解包后原始 IOException 继续抛给 okhttp,行为与未装模块一致:请求失败,不闪退。
     */
    private static Object proceedChain(Object chain, Object request) throws Throwable {
        Method proceed = findProceed(chain.getClass());
        if (proceed == null) {
            // 理论上不会发生:chain 必然实现 okhttp3.Interceptor$Chain
            return XposedHelpers.callMethod(chain, "proceed", request);
        }
        try {
            return proceed.invoke(chain, request);
        } catch (java.lang.reflect.InvocationTargetException e) {
            Throwable cause = e.getCause();
            if (cause == null) throw e;
            throw cause;
        }
    }

    /** 沿类继承链找 proceed(单参)方法;RealInterceptorChain 是 final class,直接 getMethod 即可。 */
    private static Method findProceed(Class<?> cls) {
        for (Class<?> c = cls; c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if ("proceed".equals(m.getName()) && m.getParameterCount() == 1) {
                    m.setAccessible(true);
                    return m;
                }
            }
        }
        return null;
    }

    // ---------- 1.5 去水印 ----------

    private void hookNoWatermark(XC_LoadPackage.LoadPackageParam lpparam) {
        // 作者水印:disable_watermark -> getter c() 恒 true(=不画水印)。
        // 合成器判定 if(!disableWaterMark && wm!=null) draw,见 jm7.j.a()。
        try {
            XposedHelpers.findAndHookMethod(CFG, lpparam.classLoader, "c", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    param.setResult(Boolean.TRUE);
                }
            });
            XposedBridge.log("[xhs-savefix] hooked MediaSaveConfig.c() (disableWaterMark) -> true");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook c() failed: " + t);
        }

        // AI生成标签:disable_ai -> ImageBean.u() 恒 true。仅 AIGC 帖的图带此字段,
        // 非 AI 帖无影响;EXIF 里的 AIGC 元数据仍会写入,只去掉视觉标签。
        try {
            XposedHelpers.findAndHookMethod("com.xingin.entities.ImageBean", lpparam.classLoader, "u",
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        if (!Boolean.TRUE.equals(param.getResult())) param.setResult(Boolean.TRUE);
                    }
                });
            XposedBridge.log("[xhs-savefix] hooked ImageBean.u() (disable_ai) -> true");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook ImageBean.u() failed: " + t);
        }

        // 兜底:AI 标签位图构造(混淆类 jm7.j,绑定 9.33.4)返回 null,
        // 合成器 if(!disableAi && aiBitmap!=null) 自然不画。失败不影响其他 hook。
        try {
            XposedHelpers.findAndHookMethod("jm7.j", lpparam.classLoader, "b",
                "android.content.Context", boolean.class, new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        param.setResult(null);
                    }
                });
            XposedBridge.log("[xhs-savefix] hooked jm7.j.b() (ai watermark bitmap) -> null");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook jm7.j.b() failed (non-fatal): " + t);
        }
    }

    // ---------- 2. 视频保存接口门(okhttp 拦截器) ----------

    /**
     * 只拦 /api/sns/v10/note/video/save 一个接口,改写作者关闭下载时的响应。
     *
     * 不落盘、不采集、不外发任何数据;响应体只在内存里读一次(peekBody 不消费原始流),
     * 改写完立刻交还给宿主。
     */
    private void hookVideoSaveApi(final XC_LoadPackage.LoadPackageParam lpparam) {
        final ClassLoader cl = lpparam.classLoader;
        appClassLoader = cl;
        final Class<?> interceptorCls;
        try {
            interceptorCls = cl.loadClass("okhttp3.Interceptor");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] okhttp3.Interceptor not found, skip: " + t);
            return;
        }

        // 反射代理实现 okhttp3.Interceptor,插进每个 OkHttpClient 的 application interceptors。
        final Object interceptorProxy = Proxy.newProxyInstance(cl,
            new Class<?>[]{interceptorCls}, new InvocationHandler() {
                @Override
                public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                    if (!"intercept".equals(method.getName()) || args == null || args.length != 1) {
                        // equals/hashCode/toString 等默认行为
                        if ("toString".equals(method.getName())) return "xhs-savefix-interceptor";
                        if ("hashCode".equals(method.getName())) return System.identityHashCode(proxy);
                        if ("equals".equals(method.getName())) return proxy == args[0];
                        return null;
                    }
                    Object chain = args[0];
                    Object request = XposedHelpers.callMethod(chain, "request");
                    Object response = proceedChain(chain, request);
                    try {
                        response = maybeVideoSaveFix(request, response);
                    } catch (Throwable t) {
                        XposedBridge.log("[xhs-savefix] video fix err: " + t);
                    }
                    return response;
                }
            });

        try {
            XposedHelpers.findAndHookMethod("okhttp3.OkHttpClient$Builder", cl, "build",
                new XC_MethodHook() {
                    @Override
                    @SuppressWarnings("unchecked")
                    protected void beforeHookedMethod(MethodHookParam param) {
                        try {
                            List<Object> its = (List<Object>) XposedHelpers.getObjectField(
                                param.thisObject, "interceptors");
                            if (its != null && !its.contains(interceptorProxy)) {
                                its.add(interceptorProxy);
                            }
                        } catch (Throwable t) {
                            XposedBridge.log("[xhs-savefix] inject interceptor failed: " + t);
                        }
                    }
                });
            XposedBridge.log("[xhs-savefix] armed: hooked OkHttpClient$Builder.build()");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook build() failed: " + t);
        }
    }

    // ---------- 1.8 视频保存(第三道门) ----------

    // 视频保存除了 FunctionSwitch 菜单门,还有服务端接口门:
    //   GET /api/sns/v10/note/video/save?note_id=X
    // 作者关闭时返回 {"result":0,"success":true,"data":{"msg":"作者已关闭下载功能，可尝试其他分享方式",
    //   "note_id":"X","disable":true}},没有 download_url;模型 wj6.f(disable/status/note_id/download_url/msg)。
    // 策略:平时扫 /api/sns/ 响应,缓存 noteId -> 播放用 h264 mp4 直链(video_info_v2.stream.h264[0].master_url);
    // 拦到 video/save 的 disable:true 时,改写为 disable:false 并注入 download_url,让客户端走正常下载落盘。
    private static final java.util.Map<String, String> VIDEO_URLS =
        new java.util.concurrent.ConcurrentHashMap<String, String>();
    private static final java.util.regex.Pattern P_MASTER_MP4 = java.util.regex.Pattern.compile(
        "\"h264\"\\s*:\\s*\\[.{0,4000}?\"master_url\"\\s*:\\s*\"(http[^\"]+?\\.mp4[^\"]*?)\"");
    private static final java.util.regex.Pattern P_NOTE_ID = java.util.regex.Pattern.compile(
        "\"(?:id|note_id)\"\\s*:\\s*\"([0-9a-f]{16,32})\"");

    private Object maybeVideoSaveFix(Object request, Object response) throws Throwable {
        if (request == null || response == null) return response;
        String url = String.valueOf(XposedHelpers.callMethod(request, "url"));
        // 只看 API 响应,CDN 媒体流(*.xhscdn.com)不进这里
        if (!url.contains("/api/sns/")) return response;

        Object peek = XposedHelpers.callMethod(response, "peekBody", MAX_BODY);
        String body = (String) XposedHelpers.callMethod(peek, "string");
        if (body == null || body.isEmpty()) return response;

        if (url.contains("/note/video/save")) {
            return patchVideoSaveResponse(url, body, response);
        }

        // 缓存:noteId -> h264 mp4 直链(仅内存,不落盘)
        int idx = body.indexOf("\"video_info_v2\"");
        while (idx >= 0) {
            fillVideoUrl(body, idx);
            idx = body.indexOf("\"video_info_v2\"", idx + 16);
        }
        return response;
    }

    private void fillVideoUrl(String body, int v2Idx) {
        java.util.regex.Matcher m = P_MASTER_MP4.matcher(body);
        m.region(v2Idx, Math.min(body.length(), v2Idx + 6000));
        if (!m.find()) return;
        String mp4 = m.group(1);
        int from = Math.max(0, v2Idx - 8000);
        java.util.regex.Matcher idm = P_NOTE_ID.matcher(body);
        idm.region(from, v2Idx);
        String noteId = null;
        while (idm.find()) noteId = idm.group(1); // 取离 video_info_v2 最近的一个
        if (noteId == null || VIDEO_URLS.containsKey(noteId)) return;
        if (VIDEO_URLS.size() > 500) VIDEO_URLS.clear();
        VIDEO_URLS.put(noteId, mp4);
    }

    private Object patchVideoSaveResponse(String url, String body, Object response) throws Throwable {
        if (!body.contains("\"disable\"")) return response;
        String noteId = queryParam(url, "note_id");
        String mp4 = noteId == null ? null : VIDEO_URLS.get(noteId);
        // disable 恒 false;status 必须显式为 2(客户端轮询本接口直到 status==2 才开始下载,
        // 0/1=未就绪继续轮询,其他=失败;服务端拒绝时不带 status 字段,默认 0 -> 死循环)。
        // download_url 尽力注入(有缓存时);没有也行——客户端 z6 分支会用 VideoInfo.Z(),
        // 即内存里从 videoV2 播放流构造的 h264 master_url,始终有值。
        String patched = body.replaceAll("\"disable\"\\s*:\\s*true", "\"disable\":false");
        if (patched.contains("\"status\"")) {
            patched = patched.replaceAll("\"status\"\\s*:\\s*\\d+", "\"status\":2");
        } else {
            patched = patched.replace("\"disable\":false", "\"disable\":false,\"status\":2");
        }
        if (mp4 != null) {
            java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("\"note_id\"\\s*:\\s*\"([^\"]+)\"").matcher(patched);
            if (m.find()) {
                String ins = "\"note_id\":\"" + m.group(1) + "\",\"download_url\":\"" + mp4 + "\"";
                patched = patched.substring(0, m.start()) + ins + patched.substring(m.end());
            } else {
                patched = patched.replace("\"status\":2", "\"download_url\":\"" + mp4 + "\",\"status\":2");
            }
        }
        XposedBridge.log("[xhs-savefix] video/save rewritten (disable:false + status:2"
            + (mp4 != null ? " + download_url" : "") + "), noteId=" + noteId);
        return rebuildResponse(response, patched);
    }

    private Object rebuildResponse(Object response, String text) throws Throwable {
        Object oldBody = XposedHelpers.callMethod(response, "body");
        Object mediaType = oldBody == null ? null : XposedHelpers.callMethod(oldBody, "contentType");
        Object newBody = createResponseBody(mediaType, text);
        if (newBody == null) {
            XposedBridge.log("[xhs-savefix] ResponseBody.create failed, keep original");
            return response;
        }
        Object builder = XposedHelpers.callMethod(response, "newBuilder");
        XposedHelpers.callMethod(builder, "body", newBody);
        return XposedHelpers.callMethod(builder, "build");
    }

    // okhttp3(未混淆)三种 create 签名依次尝试:Kotlin companion、(MediaType,String)、(String,MediaType)
    private static volatile ClassLoader appClassLoader;

    private Object createResponseBody(Object mediaType, String text) {
        ClassLoader cl = appClassLoader;
        try {
            Class<?> c = XposedHelpers.findClass("okhttp3.ResponseBody", cl);
            Object companion = XposedHelpers.getStaticObjectField(c, "Companion");
            return XposedHelpers.callMethod(companion, "create", text, mediaType);
        } catch (Throwable ignored) {}
        try {
            Class<?> c = XposedHelpers.findClass("okhttp3.ResponseBody", cl);
            return XposedHelpers.callStaticMethod(c, "create", mediaType, text);
        } catch (Throwable ignored) {}
        try {
            Class<?> c = XposedHelpers.findClass("okhttp3.ResponseBody", cl);
            return XposedHelpers.callStaticMethod(c, "create", text, mediaType);
        } catch (Throwable ignored) {}
        return null;
    }

    private static String queryParam(String url, String key) {
        int i = url.indexOf('?' + key + '=');
        if (i < 0) i = url.indexOf('&' + key + '=');
        if (i < 0) return null;
        int s = i + key.length() + 2;
        int e = url.indexOf('&', s);
        if (e < 0) e = url.length();
        return url.substring(s, e);
    }

    // 视频下载分支纠偏:wh8.o1.a(activity, aVar, noteFeed, fVar, w0Var, int, z6, oVar) 里
    // if(z6) 用 noteFeed.s2().Z()(VideoInfo.url,别人的视频服务端不下发,是空的)
    // else    用 fVar.b()(wj6.f.download_url,官方/我们注入的直链)。
    // 我们把 disableWaterMark 强制 true 后 z6 恒为 true,别人的视频就拿到空 URL -> 进度卡 0。
    // 这里在 Z() 为空且 download_url 非空时把 z6 翻成 false,强制走直链。
    // (混淆类/方法名,绑定 9.33.4;失败只影响视频保存,不影响其他功能)
    private void hookVideoDownloadBranch(XC_LoadPackage.LoadPackageParam lpparam) {
        try {
            XposedHelpers.findAndHookMethod("wh8.o1", lpparam.classLoader, "a",
                "android.app.Activity", "jra.a", "com.xingin.entities.notedetail.NoteFeed",
                "wj6.f", "sv9.w0", int.class, boolean.class, "y06.o",
                new XC_MethodHook() {
                    @Override
                    protected void beforeHookedMethod(MethodHookParam param) {
                        Object[] a = param.args;
                        if (a == null || a.length < 8 || !Boolean.TRUE.equals(a[6])) return;
                        String z = null;
                        try {
                            Object vi = XposedHelpers.callMethod(a[2], "s2");
                            if (vi != null) {
                                Object r = XposedHelpers.callMethod(vi, "Z");
                                z = r == null ? null : String.valueOf(r);
                            }
                        } catch (Throwable ignored) {}
                        if (z != null && !z.isEmpty()) return; // 自己的视频:Z() 有值,保持原分支
                        String dl = null;
                        try {
                            Object r = XposedHelpers.callMethod(a[3], "b");
                            dl = r == null ? null : String.valueOf(r);
                        } catch (Throwable ignored) {}
                        if (dl != null && !dl.isEmpty()) {
                            a[6] = Boolean.FALSE;
                            XposedBridge.log("[xhs-savefix] o1.a: Z() empty, use download_url=" + dl);
                        }
                    }
                });
            XposedBridge.log("[xhs-savefix] hooked wh8.o1.a (video download branch)");
        } catch (Throwable t) {
            XposedBridge.log("[xhs-savefix] hook wh8.o1.a failed (non-fatal): " + t);
        }
    }
}
