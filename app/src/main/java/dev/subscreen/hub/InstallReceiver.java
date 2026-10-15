package dev.subscreen.hub;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

import org.json.JSONObject;

/**
 * Status of an update the app is installing over itself. Android first asks
 * for the user's confirmation (STATUS_PENDING_USER_ACTION carries the prompt
 * to show); a failure is passed back to the page so the menu can say so.
 */
public class InstallReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            return;
        }
        if (status == PackageInstaller.STATUS_SUCCESS) return;   // the app restarts on the new build
        MainActivity activity = MainActivity.current.get();
        if (activity != null) {
            String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            activity.js("window.onUpdateProgress && window.onUpdateProgress(-1, "
                    + JSONObject.quote(msg == null || msg.isEmpty() ? "Install was cancelled" : "Install failed: " + msg) + ")");
        }
    }
}
