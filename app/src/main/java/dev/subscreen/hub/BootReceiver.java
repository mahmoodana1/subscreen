package dev.subscreen.hub;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.provider.Settings;

/**
 * Brings Subscreen back after a reboot or an update. Android only lets an app
 * open its window from the background when it may "display over other apps";
 * without that permission this does nothing, and choosing Subscreen as the
 * home app covers the reboot case instead.
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        // Alarms do not survive a reboot or an update; re-arm the next athan.
        MainActivity.armNextAthan(context);
        if (!Settings.canDrawOverlays(context)) return;
        context.startActivity(new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
    }
}
