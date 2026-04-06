// ─── WordComet Config ───────────────────────────────────
// Change this to your deployed server URL
const CONFIG = {
  API_BASE: "https://wordcomet.shyapsneon.tech", 
  GCM_SENDER_ID: "218267654833",

  // Firebase config — get from Firebase Console → Project Settings → Web App
  FIREBASE: {
    apiKey: "YOUR_API_KEY",
    authDomain: "YOUR_PROJECT.firebaseapp.com",
    projectId: "YOUR_PROJECT_ID",
    storageBucket: "YOUR_PROJECT.appspot.com",
    messagingSenderId: "YOUR_SENDER_ID",
    appId: "YOUR_APP_ID",
  },

  // FCM VAPID key — Firebase Console → Cloud Messaging → Web Push certificates
  VAPID_KEY: "BEIfJgFyAo6e20IAVx2KCOWpLKVpOUmk8JOtlwxxl_1G1ItFqzYr43FuRHWdLuaTSG1-lQECYvmDvDaRA0cB8lE",
};
