import asyncio
from main import YTDLSource

async def main():
    source = await YTDLSource.from_url("ytsearch1:karma police", stream=False)
    print("created", source)
    print("title", getattr(source, "title", None))
    source.cleanup()

asyncio.run(main())
