from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

class ResponseFilters(BaseModel):
    include_markdown: bool = Field(True, description="Enthält das saubere Markdown im finalen JSON.")
    include_html: bool = Field(False, description="Enthält das rohe/gerenderte HTML der Seite.")
    include_extracted_data: bool = Field(True, description="Enthält extrahierte strukturierte Daten wie Links und Tabellen.")
    include_screenshot: bool = Field(False, description="Generiert und hostet temporär einen Screenshot der Seite.")

    class Config:
        json_schema_extra = {
            "example": {
                "include_markdown": True,
                "include_html": False,
                "include_extracted_data": True,
                "include_screenshot": False
            }
        }

class ScrapeRequest(BaseModel):
    url: str = Field(..., description="Die exakte URL der Webseite, die gescraped werden soll.", example="https://example.com")
    delivery_mode: str = Field("direct", description="Übertragungsmodus: 'direct' (synchron), 'webhook' (asynchron) oder 'both'.", example="direct")
    webhook_url: Optional[str] = Field(None, description="Die URL, an die die Daten nach Abschluss des Scrapings per POST gesendet werden (erforderlich bei 'webhook'/'both').", example="https://yourdomain.com/webhook")
    response_filters: Optional[ResponseFilters] = Field(default_factory=ResponseFilters, description="Steuert, welche Teile des Payloads zurückgegeben werden sollen, um Token zu sparen.")
    block_media: bool = Field(True, description="Blockiert Bilder, Fonts und Stylesheets, um die Ladezeit massiv zu beschleunigen.")
    wait_for_selector: Optional[str] = Field(None, description="Ein optionaler CSS-Selector, auf den gewartet wird, bevor der Inhalt extrahiert wird.", example="#content-loaded")
    wait_until: str = Field("auto", description="Warte-Bedingung für dynamische Inhalte: 'auto' (intelligentes Kurz-Warten auf Netzwerkruhe), 'networkidle' (vollständig), 'load' oder 'domcontentloaded'.", example="auto")
    page_crawling: bool = Field(False, description="Aktiviert das automatische Scraping von verlinkten Unterseiten derselben Domain.")
    max_crawl_depth: int = Field(1, description="Die maximale Tiefe für das Crawling von Unterseiten (1 = nur direkt verlinkte Seiten).")
    max_crawl_pages: int = Field(5, description="Das absolute Limit für die Anzahl an Unterseiten, die gescraped werden sollen.")
    chunk_size: Optional[int] = Field(None, description="Zerschneidet das Markdown nach dieser Zeichenanzahl in Chunks für Vektordatenbanken.", example=1000)
    chunk_overlap: Optional[int] = Field(None, description="Der Überlappungsbereich (in Zeichen) zwischen aufeinanderfolgenden Chunks.", example=200)
    custom_headers: Optional[Dict[str, str]] = Field(None, description="Benutzerdefinierte HTTP-Header, die beim Request mitgesendet werden.")
    custom_cookies: Optional[List[Dict[str, Any]]] = Field(None, description="Session-Cookies zur Authentifizierung auf geschützten Seiten. Format: [{'name': 'x', 'value': 'y', 'domain': 'z'}].")

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://example.com",
                "delivery_mode": "direct",
                "webhook_url": None,
                "response_filters": {
                    "include_markdown": True,
                    "include_html": False,
                    "include_extracted_data": True,
                    "include_screenshot": False
                },
                "block_media": True,
                "wait_for_selector": None,
                "wait_until": "auto",
                "page_crawling": False,
                "max_crawl_depth": 1,
                "max_crawl_pages": 5,
                "chunk_size": None,
                "chunk_overlap": None,
                "custom_headers": None,
                "custom_cookies": None
            }
        }

class Metadata(BaseModel):
    url: str = Field(..., description="Die gescrapte Ziel-URL.")
    title: Optional[str] = Field(None, description="Der Seitentitel der geladenen Seite.")
    description: Optional[str] = Field(None, description="Die Meta-Beschreibung der Seite für SEO.")
    status: int = Field(..., description="Der empfangene HTTP-Statuscode der Seite (z.B. 200).")
    execution_time_ms: int = Field(..., description="Die gemessene Verarbeitungszeit in Millisekunden.")
    status_detail: Optional[str] = Field(None, description="Zusätzliche Details zum Status, z.B. Login-Wall-Meldungen.")

class ExtractedData(BaseModel):
    links: List[str] = Field(default_factory=list, description="Liste aller gefundenen Links auf der Seite, konvertiert in absolute URLs.")
    tables: List[List[Dict[str, Any]]] = Field(default_factory=list, description="Liste extrahierter HTML-Tabellen, strukturiert als JSON.")
    images: List[str] = Field(default_factory=list, description="Liste aller gefundenen Bild-URLs auf der Seite.")

class ScrapeResponse(BaseModel):
    meta: Metadata = Field(..., description="Zusammenfassende Metadaten der Anfrage.")
    content_md: Optional[str] = Field(None, description="Der extrahierte Seitentext als gesäubertes Markdown.")
    html: Optional[str] = Field(None, description="Der rohe/gerenderte HTML-Code der Seite.")
    extracted_data: Optional[ExtractedData] = Field(None, description="Strukturierte Daten wie Links, Tabellen und Medien-URLs.")
    screenshot_url: Optional[str] = Field(None, description="Ein temporärer Link zum generierten Screenshot der Seite.")
    chunks: Optional[List[str]] = Field(None, description="Das zerschnittene Markdown, falls Chunking angefordert wurde.")
    crawled_pages: Optional[List[Dict[str, Any]]] = Field(None, description="Scraping-Ergebnisse der gecrawlten Unterseiten.")
