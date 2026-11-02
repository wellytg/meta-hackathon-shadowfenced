import asyncio, json, urllib.request
from playwright.async_api import async_playwright
EXT=r"C:\meta-hackathon\work7\extfix\extension\dist\chrome-dev"
TOKEN=json.loads(urllib.request.urlopen("http://127.0.0.1:8765/api/ext/pairing").read())["token"]
async def main():
    async with async_playwright() as p:
        ctx=await p.chromium.launch_persistent_context(r"C:\meta-hackathon\work7\extdemo_profile2",headless=False,args=[f"--disable-extensions-except={EXT}",f"--load-extension={EXT}"],viewport={"width":1280,"height":760})
        sw=ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        eid=sw.url.split("/")[2]; pop=await ctx.new_page(); await pop.goto(f"chrome-extension://{eid}/src/popup.html"); await pop.fill("#token",TOKEN); await pop.click("#save"); await pop.wait_for_timeout(800); await pop.close()
        pg=await ctx.new_page(); logs=[]; pg.on("console",lambda m: logs.append(m.type+": "+m.text))
        await pg.goto("http://127.0.0.1:8000/"); await pg.wait_for_timeout(1500)
        await pg.click("#composer-text"); await pg.keyboard.type("Draft a reply to Ada Byron, ada.byron@northwind-mail.co, phone +44 20 7946 0958. Also deploy with AKIAIOSFODNN7EXAMPLE", delay=25); await pg.wait_for_timeout(1500)
        await pg.click("#sendBtn")
        for t in (200,700,2000):
            await pg.wait_for_timeout(t); await pg.screenshot(path=f"extdemo/sendcard_{t}.png")
        print("log text:", await pg.inner_text("#log"))
        print("hosts:", await pg.evaluate("[...document.querySelectorAll('body > *')].map(e=>e.tagName+'#'+e.id+'.'+e.className).slice(-6)"))
        print("console:", logs[-10:])
        await ctx.close()
asyncio.run(main())
