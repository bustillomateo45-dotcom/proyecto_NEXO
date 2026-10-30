/* =========================================================
   NEXO - service worker
   Recibe los avisos PUSH del servidor y muestra la notificación
   aunque la página o el navegador estén cerrados.
   Al tocar la notificación se abre NEXO: si ya hay una ventana abierta
   la trae al frente; si no, abre una nueva.
   ========================================================= */

self.addEventListener("install", function () { self.skipWaiting(); });

self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (event) {
  var datos = {};

  try {
    datos = event.data ? event.data.json() : {};
  } catch (e) {
    datos = { cuerpo: event.data ? event.data.text() : "" };
  }

  var titulo = datos.titulo || "NEXO";

  var opciones = {
    body: datos.cuerpo || "Tienes un mensaje nuevo",
    tag: datos.tag || "nexo",
    renotify: true,              // vuelve a avisar aunque ya haya otra del mismo canal
    silent: false,               // usa el sonido del dispositivo
    vibrate: [120, 60, 120],
    data: { canal: datos.canal }
  };

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (ventanas) {
      // si está mirando la página en este momento, ella misma avisa (aviso emergente + sonido)
      var viendo = ventanas.some(function (v) {
        return v.visibilityState === "visible" && v.focused;
      });
      if (viendo) return;
      return self.registration.showNotification(titulo, opciones);
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (ventanas) {
      // si NEXO ya está abierto en alguna pestaña o ventana, se trae al frente
      for (var i = 0; i < ventanas.length; i++) {
        if ("focus" in ventanas[i]) return ventanas[i].focus();
      }
      // si no, se abre; "/" lleva al panel de quien tiene la sesión guardada
      return self.clients.openWindow("/");
    })
  );
});