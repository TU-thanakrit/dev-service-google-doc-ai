from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    project_id: str = Field(validation_alias="GOOGLE_CLOUD_PROJECT")
    docai_processor_id: str = Field(validation_alias="DOCAI_PROCESSOR_ID")
    docai_location: str = Field(
        default="asia-southeast1",
        validation_alias="DOCAI_LOCATION",
    )
    vertex_location: str = Field(
        default="asia-southeast1",
        validation_alias="VERTEX_LOCATION",
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        validation_alias="GEMINI_MODEL",
    )
