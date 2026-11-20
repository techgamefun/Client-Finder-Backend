from pydantic import BaseModel


class ImageAssetResponse(BaseModel):
    url: str
