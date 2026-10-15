package dev.subscreen.hub;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.view.accessibility.AccessibilityEvent;

/**
 * Turns the screen off (and opens the power menu) for the app. Android only
 * lets a regular app do this through an accessibility service, which is how
 * launchers implement "double-tap to lock". It reads nothing from other apps.
 */
public class ScreenService extends AccessibilityService {
    static ScreenService instance;

    @Override protected void onServiceConnected() { instance = this; }

    @Override public boolean onUnbind(Intent intent) { instance = null; return super.onUnbind(intent); }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) { }

    @Override public void onInterrupt() { }

    static boolean lockScreen() {
        return instance != null && instance.performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN);
    }

    static boolean powerMenu() {
        return instance != null && instance.performGlobalAction(GLOBAL_ACTION_POWER_DIALOG);
    }
}
