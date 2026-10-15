package dev.subscreen.hub;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageInfo;
import android.content.res.AssetFileDescriptor;
import android.graphics.Color;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.audiofx.LoudnessEnhancer;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.webkit.WebViewAssetLoader;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.ref.WeakReference;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The phone side of Subscreen: one full-screen WebView showing the page that
 * ships inside the app (the repo's web/ folder), kept awake and in landscape.
 *
 * The page talks to the computers itself. This class only adds what a web
 * page cannot do: stay on screen, start after a reboot, and play the athan
 * loudly without needing a tap first.
 */
public class MainActivity extends Activity {
    private static final String START = "https://appassets.androidplatform.net/assets/index.html";
    /** CI replaces this release with every build; its title carries the build number. */
    private static final String RELEASE_API = "https://api.github.com/repos/mahmoodana1/subscreen/releases/tags/latest";
    static final String INSTALL_ACTION = "dev.subscreen.hub.INSTALL_STATUS";

    static WeakReference<MainActivity> current = new WeakReference<>(null);

    private WebView web;
    private final Handler main = new Handler(Looper.getMainLooper());
    private MediaPlayer athan;
    private LoudnessEnhancer louder;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            getWindow().getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }

        final WebViewAssetLoader assets = new WebViewAssetLoader.Builder()
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        // The page is served from a secure origin but the computers answer over
        // plain HTTP inside the tailnet.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return assets.shouldInterceptRequest(request.getUrl());
            }
        });
        web.addJavascriptInterface(new Bridge(), "Subscreen");
        setContentView(web);
        web.loadUrl(START);
        current = new WeakReference<>(this);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) immersive();
    }

    @Override
    protected void onResume() {
        super.onResume();
        immersive();
        if (web != null) web.onResume();
    }

    @Override
    protected void onPause() {
        if (web != null) web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        stopAthan();
        if (web != null) web.destroy();
        super.onDestroy();
    }

    // A kiosk: Back does nothing rather than leaving the app.
    @SuppressLint("MissingSuperCall")
    @Override
    public void onBackPressed() { }

    private void immersive() {
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    | View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    // ------------------------------------------------------------------ athan

    private void playAthan() {
        stopAthan();
        try {
            AssetFileDescriptor fd = getAssets().openFd("athan.mp3");
            athan = new MediaPlayer();
            athan.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            athan.setDataSource(fd.getFileDescriptor(), fd.getStartOffset(), fd.getLength());
            fd.close();
            athan.prepare();
            // Same idea as the old subscreen's gain stage: about +12 dB of
            // loudness, faded in over 7 seconds.
            louder = new LoudnessEnhancer(athan.getAudioSessionId());
            louder.setTargetGain(1200);
            louder.setEnabled(true);
            athan.setVolume(0f, 0f);
            athan.setOnCompletionListener(mp -> {
                stopAthan();
                web.evaluateJavascript("window.onAthanEnded && window.onAthanEnded()", null);
            });
            athan.start();
            final long started = System.currentTimeMillis();
            final MediaPlayer mine = athan;
            main.post(new Runnable() {
                @Override public void run() {
                    if (athan != mine) return;
                    float k = Math.min(1f, (System.currentTimeMillis() - started) / 7000f);
                    try { mine.setVolume(k, k); } catch (IllegalStateException e) { return; }
                    if (k < 1f) main.postDelayed(this, 100);
                }
            });
        } catch (Exception e) {
            stopAthan();
        }
    }

    private void stopAthan() {
        if (louder != null) { try { louder.release(); } catch (Exception ignored) { } louder = null; }
        if (athan != null) {
            try { athan.stop(); } catch (Exception ignored) { }
            athan.release();
            athan = null;
        }
    }

    // ------------------------------------------------------------------ rest

    /** Resting is a black page with the backlight at its lowest; waking restores the user's brightness. */
    private void setRest(boolean on) {
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = on ? 0.004f : WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(lp);
    }

    // ------------------------------------------------------------------ updates

    void js(String code) {
        main.post(() -> { if (web != null) web.evaluateJavascript(code, null); });
    }

    private int buildNumber() {
        try {
            PackageInfo p = getPackageManager().getPackageInfo(getPackageName(), 0);
            return (int) (Build.VERSION.SDK_INT >= 28 ? p.getLongVersionCode() : p.versionCode);
        } catch (Exception e) {
            return 0;
        }
    }

    private static HttpURLConnection open(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(30000);
        c.setInstanceFollowRedirects(true);
        c.setRequestProperty("User-Agent", "subscreen-app");
        return c;
    }

    /** Asks GitHub for the newest build and reports it to the page. */
    private void checkUpdate() {
        new Thread(() -> {
            int latest = -1;
            String url = null;
            try {
                HttpURLConnection c = open(RELEASE_API);
                c.setRequestProperty("Accept", "application/vnd.github+json");
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                try (InputStream in = c.getInputStream()) {
                    byte[] b = new byte[8192];
                    for (int n; (n = in.read(b)) > 0; ) buf.write(b, 0, n);
                }
                JSONObject rel = new JSONObject(buf.toString("UTF-8"));
                Matcher m = Pattern.compile("build (\\d+)").matcher(rel.optString("name"));
                if (m.find()) latest = Integer.parseInt(m.group(1));
                JSONArray assets = rel.optJSONArray("assets");
                for (int i = 0; assets != null && i < assets.length(); i++) {
                    JSONObject a = assets.getJSONObject(i);
                    if (a.optString("name").endsWith(".apk")) url = a.optString("browser_download_url");
                }
            } catch (Exception e) {
                latest = -1;
            }
            js("window.onUpdateInfo && window.onUpdateInfo(" + buildNumber() + "," + latest + ","
                    + (url == null ? "null" : JSONObject.quote(url)) + ")");
        }).start();
    }

    /** Downloads the APK into a PackageInstaller session; Android then shows its install prompt. */
    private void installUpdate(String url) {
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            js("window.onUpdateProgress && window.onUpdateProgress(-1, 'Allow Subscreen to install apps, then tap again')");
            startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            return;
        }
        new Thread(() -> {
            try {
                PackageInstaller pi = getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams params =
                        new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                params.setAppPackageName(getPackageName());
                int id = pi.createSession(params);
                try (PackageInstaller.Session session = pi.openSession(id)) {
                    HttpURLConnection c = open(url);
                    long total = c.getContentLengthLong(), done = 0;
                    int shown = -1;
                    try (InputStream in = c.getInputStream();
                         OutputStream out = session.openWrite("subscreen.apk", 0, total > 0 ? total : -1)) {
                        byte[] b = new byte[65536];
                        for (int n; (n = in.read(b)) > 0; ) {
                            out.write(b, 0, n);
                            done += n;
                            int pct = total > 0 ? (int) (done * 99 / total) : 0;
                            if (pct != shown) { shown = pct; js("window.onUpdateProgress && window.onUpdateProgress(" + pct + ")"); }
                        }
                        session.fsync(out);
                    }
                    Intent status = new Intent(INSTALL_ACTION).setPackage(getPackageName());
                    PendingIntent pending = PendingIntent.getBroadcast(this, id, status,
                            PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0));
                    session.commit(pending.getIntentSender());
                }
                js("window.onUpdateProgress && window.onUpdateProgress(100)");
            } catch (Exception e) {
                js("window.onUpdateProgress && window.onUpdateProgress(-1, " + JSONObject.quote("Update failed: " + e.getMessage()) + ")");
            }
        }).start();
    }

    // ------------------------------------------------------------------ bridge

    /** What the page can call as window.Subscreen.*. */
    private class Bridge {
        @JavascriptInterface public void playAthan() { main.post(MainActivity.this::playAthan); }

        @JavascriptInterface public void stopAthan() { main.post(MainActivity.this::stopAthan); }

        @JavascriptInterface public boolean athanPlaying() { return athan != null; }

        @JavascriptInterface public String version() {
            try {
                PackageInfo p = getPackageManager().getPackageInfo(getPackageName(), 0);
                return p.versionName;
            } catch (Exception e) {
                return "?";
            }
        }

        /** Opens Android's "default home app" setting, where Subscreen can be chosen. */
        @JavascriptInterface public void openHomeSettings() {
            main.post(() -> startActivity(new Intent(Settings.ACTION_HOME_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)));
        }

        /** Asks for "display over other apps", which lets the app start itself after a reboot. */
        @JavascriptInterface public void requestStartOnBoot() {
            main.post(() -> startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)));
        }

        @JavascriptInterface public boolean canStartOnBoot() { return Settings.canDrawOverlays(MainActivity.this); }

        @JavascriptInterface public void setRest(boolean on) { main.post(() -> MainActivity.this.setRest(on)); }

        @JavascriptInterface public int build() { return buildNumber(); }

        @JavascriptInterface public void checkUpdate() { MainActivity.this.checkUpdate(); }

        @JavascriptInterface public void installUpdate(String url) { main.post(() -> MainActivity.this.installUpdate(url)); }
    }
}
