package dev.subscreen.hub;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
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
    }
}
