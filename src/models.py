import re
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

class PropertyObject(BaseModel):
    id: str
    url: str
    title: Optional[str] = ""
    name: Optional[str] = Field(None, description="Property title or headline")
    lat: Optional[float] = Field(None, description="Latitude coordinate")
    lon: Optional[float] = Field(None, description="Longitude coordinate")
    price: Optional[int] = Field(None, description="Price in Euros as an integer")
    size: Optional[int] = Field(None, description="Property size in square meters")
    price_per_sqm: Optional[float] = Field(None, alias="pricePerSqm", description="Price per square meter (€/m²)")
    description: Optional[str] = Field(None, description="Full description or location context")
    location_text: Optional[str] = Field(None, alias="locationText")
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    advertiser: Optional[str] = None
    is_approximate_location: bool = Field(False, alias="isApproximateLocation")
    location_notice: Optional[str] = Field(None, alias="locationNotice")

    @field_validator('price', mode='before')
    def parse_price(cls, v):
        if isinstance(v, str):
            cleaned = re.sub(r'[^\d]', '', v)
            return int(cleaned) if cleaned else None
        return v

    @field_validator('size', mode='before')
    def parse_size(cls, v):
        if isinstance(v, str):
            cleaned = re.sub(r'[^\d]', '', v)
            return int(cleaned) if cleaned else None
        return v

    @model_validator(mode='after')
    def calculate_price_per_sqm(self):
        """Automatically calculates price/sqm if both price and size exist."""
        if self.price and self.size and self.size > 0:
            self.price_per_sqm = round(self.price / self.size, 2)
        return self

    @classmethod
    def from_scraped_dict(cls, data: dict):
        """Factory method to map your raw JSON dictionary fields to the Pydantic model."""
        features = data.get("features", [])
        raw_size = next((f for f in features if "m²" in f), None)
        
        return cls(
            id=data.get("id"),
            url=data.get("url"),
            title=data.get("title"),
            name=data.get("title") or data.get("locationText"),
            lat=data.get("latitude"),
            lon=data.get("longitude"),
            price=data.get("price"),
            size=raw_size,
            description=data.get("locationNotice") or data.get("locationText"),
            locationText=data.get("locationText"),
            neighborhood=data.get("neighborhood"),
            city=data.get("city"),
            advertiser=data.get("advertiser"),
            isApproximateLocation=data.get("isApproximateLocation", False),
            locationNotice=data.get("locationNotice")
        )

    class Config:
        populate_by_name = True