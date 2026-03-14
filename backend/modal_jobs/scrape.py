# backend/modal_jobs/scrape.py
import modal

app = modal.App("planning-scrapers")

volume = modal.Volume.from_name("planning-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install("browser-use", "python-dotenv")
    .run_commands(
        "uvx browser-use install",
    )
)

SCRAPERS = {
    "haringey": "backend.scrapers.haringey:scrape_haringey",
    "hackney": "backend.scrapers.hackney:scrape_hackney",
}


@app.function(
    image=image,
    volumes={"/data": volume},
    timeout=600,
    secrets=[modal.Secret.from_name("vllm-config")],
)
async def scrape_council(council: str):
    import importlib
    import os

    module_path, func_name = SCRAPERS[council].rsplit(":", 1)
    module = importlib.import_module(module_path)
    scrape_fn = getattr(module, func_name)

    await scrape_fn(
        vllm_endpoint=os.environ["VLLM_ENDPOINT"],
        vllm_api_key=os.environ["VLLM_API_KEY"],
        vllm_model=os.environ["VLLM_MODEL"],
        output_dir="/data",
    )

    volume.commit()
    print(f"Committed data for {council} to volume")


@app.local_entrypoint()
async def main(council: str = "haringey"):
    await scrape_council.remote.aio(council)
