[1mdiff --git a/static/topbar.html b/static/topbar.html[m
[1mindex 68ad75f..aad5a28 100644[m
[1m--- a/static/topbar.html[m
[1m+++ b/static/topbar.html[m
[36m@@ -1,5 +1,5 @@[m
 <style>[m
[31m-    .top-nav {[m
[32m+[m[32m.top-nav {[m
     width: 100%;[m
     margin-bottom: 28px;[m
     padding: 14px 18px;[m
[36m@@ -8,12 +8,11 @@[m
     align-items: center;[m
     justify-content: space-between;[m
 [m
[31m-    background: rgba(20, 20, 31, 0.72);[m
[31m-    border: 1px solid var(--border);[m
[32m+[m[32m    background: #ffffff;[m
[32m+[m[32m    border: 1px solid #dbeaf2;[m
     border-radius: 16px;[m
 [m
[31m-    backdrop-filter: blur(14px);[m
[31m-    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.2);[m
[32m+[m[32m    box-shadow: 0 10px 30px rgba(8, 116, 185, 0.12);[m
 }[m
 [m
 .nav-brand {[m
[36m@@ -23,33 +22,14 @@[m
     text-decoration: none;[m
 }[m
 [m
[31m-.nav-brand-icon {[m
[31m-    width: 36px;[m
[31m-    height: 36px;[m
[31m-[m
[31m-    display: flex;[m
[31m-    align-items: center;[m
[31m-    justify-content: center;[m
[31m-[m
[32m+[m[32m.nav-brand-logo {[m
[32m+[m[32m    width: 110px;[m
[32m+[m[32m    height: 62px;[m
[32m+[m[32m    display: block;[m
[32m+[m[32m    object-fit: contain;[m
     border-radius: 10px;[m
[31m-    background: linear-gradient([m
[31m-        135deg,[m
[31m-        var(--accent),[m
[31m-        var(--accent-secondary, #ff6584)[m
[31m-    );[m
[31m-[m
[31m-    font-size: 17px;[m
[31m-}[m
[31m-[m
[31m-.nav-brand-text {[m
[31m-    font-family: "Syne", sans-serif;[m
[31m-    font-size: 1.1rem;[m
[31m-    font-weight: 800;[m
[31m-    color: var(--text);[m
[31m-}[m
 [m
[31m-.nav-brand-text span {[m
[31m-    color: var(--accent);[m
[32m+[m[32m    box-shadow: 0 4px 14px rgba(8, 116, 185, 0.25);[m
 }[m
 [m
 .nav-links {[m
[36m@@ -57,7 +37,6 @@[m
     align-items: center;[m
     gap: 8px;[m
 }[m
[31m-[m
 .nav-link {[m
     padding: 9px 13px;[m
 [m
[36m@@ -65,11 +44,11 @@[m
     border-radius: 10px;[m
 [m
     background: transparent;[m
[31m-    color: var(--muted);[m
[32m+[m[32m    color: #526475;[m
 [m
     font-family: "DM Sans", sans-serif;[m
     font-size: 0.88rem;[m
[31m-    font-weight: 500;[m
[32m+[m[32m    font-weight: 600;[m
 [m
     text-decoration: none;[m
     cursor: pointer;[m
[36m@@ -81,26 +60,26 @@[m
 }[m
 [m
 .nav-link:hover {[m
[31m-    color: var(--text);[m
[31m-    background: rgba(255, 255, 255, 0.06);[m
[31m-    border-color: rgba(255, 255, 255, 0.08);[m
[32m+[m[32m    color: #0874b9;[m
[32m+[m[32m    background: #edf8fd;[m
[32m+[m[32m    border-color: #bce5f5;[m
 }[m
 [m
 .nav-link.active {[m
[31m-    color: var(--text);[m
[31m-    background: var(--accent-soft);[m
[31m-    border-color: rgba(108, 99, 255, 0.35);[m
[31m-    box-shadow: inset 0 -2px 0 var(--accent-secondary);[m
[32m+[m[32m    color: #0874b9;[m
[32m+[m[32m    background: #e8f7fd;[m
[32m+[m[32m    border-color: #8dd6ef;[m
[32m+[m[32m    box-shadow: inset 0 -3px 0 #ff9518;[m
 }[m
 [m
 .nav-logout {[m
[31m-    color: #ff8fa3;[m
[32m+[m[32m    color: #f05a28;[m
 }[m
 [m
 .nav-logout:hover {[m
     color: #ffffff;[m
[31m-    background: rgba(255, 101, 132, 0.15);[m
[31m-    border-color: rgba(255, 101, 132, 0.25);[m
[32m+[m[32m    background: #f05a28;[m
[32m+[m[32m    border-color: #f05a28;[m
 }[m
 [m
 @media (max-width: 700px) {[m
[36m@@ -125,13 +104,11 @@[m
 [m
     <a class="nav-brand" href="dashboard.html">[m
 [m
[31m-        <div class="nav-brand-icon">[m
[31m-            ☄️[m
[31m-        </div>[m
[31m-[m
[31m-        <div class="nav-brand-text">[m
[31m-            Word<span>Comet</span>[m
[31m-        </div>[m
[32m+[m[32m        <img[m
[32m+[m[32m            class="nav-brand-logo"[m
[32m+[m[32m            src="word_images/wordcomet-logo-final.png"[m
[32m+[m[32m            alt="WordComet"[m
[32m+[m[32m        >[m
 [m
     </a>[m
 [m
