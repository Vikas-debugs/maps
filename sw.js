// sw.js — service worker for notification delivery + interaction handling
self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(self.clients.claim()); });

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "notify"){
    self.registration.showNotification(data.title || "Update", {
      body: data.body || "",
      tag: "maps-live-" + Date.now(),
      requireInteraction: false,
      silent: !!data.silent,
      icon: data.icon || undefined,
      badge: data.badge || undefined
    });
  }
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list){
        if (c.url.indexOf(self.location.origin) === 0){
          c.focus();
          return;
        }
      }
      if (self.clients.openWindow){
        return self.clients.openWindow("./");
      }
    })
  );
});