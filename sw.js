const C="asx-v1";
self.addEventListener("install",e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(["./","index.html","manifest.json"])));self.skipWaiting()});
self.addEventListener("fetch",e=>{const u=e.request.url;
 e.respondWith(fetch(e.request).then(r=>{if(u.includes("data.json")){const x=r.clone();caches.open(C).then(c=>c.put(e.request,x))}return r}).catch(()=>caches.match(e.request)))});
