from app.adapters.builtin import REGISTRY, get_adapter
from app.adapters.cro_open_data import CROOpenDataAdapter
from app.adapters.domain_rdap import DomainRDAPAdapter
from app.adapters.domain_relationships import DomainRelationshipAdapter
from app.adapters.duckduckgo_instant import DuckDuckGoInstantAdapter
from app.adapters.entra_tenant import EntraTenantMetadataAdapter
from app.adapters.federated_identity import FederatedIdentityAdapter
from app.adapters.gallery_dl import GalleryDLAdapter
from app.adapters.github_developer_identity import GitHubDeveloperIdentityAdapter
from app.adapters.github_discovery import GitHubDiscoveryAdapter
from app.adapters.gleif import GLEIFAdapter
from app.adapters.gravatar_public import GravatarPublicAdapter
from app.adapters.openalex_scholar import OpenAlexScholarAdapter
from app.adapters.otx_historical import OTXHistoricalURLAdapter
from app.adapters.pgp_public import PGPPublicAdapter
from app.adapters.phone_offline import PhoneOfflineAdapter
from app.adapters.phone_presence import PhonePresenceAdapter
from app.adapters.site_intelligence import LiveSiteIntelligenceAdapter
from app.adapters.snapchat_public import SnapchatPublicAdapter
from app.adapters.telegram_public import TelegramPublicAdapter
from app.adapters.tiktok_public import TikTokPublicAdapter
from app.adapters.username_public import UsernamePublicProfilesAdapter

REGISTRY[TelegramPublicAdapter.tool_id] = TelegramPublicAdapter
REGISTRY[SnapchatPublicAdapter.tool_id] = SnapchatPublicAdapter
REGISTRY[CROOpenDataAdapter.tool_id] = CROOpenDataAdapter
REGISTRY[TikTokPublicAdapter.tool_id] = TikTokPublicAdapter
REGISTRY[DuckDuckGoInstantAdapter.tool_id] = DuckDuckGoInstantAdapter
REGISTRY[EntraTenantMetadataAdapter.tool_id] = EntraTenantMetadataAdapter
REGISTRY[FederatedIdentityAdapter.tool_id] = FederatedIdentityAdapter
REGISTRY[GLEIFAdapter.tool_id] = GLEIFAdapter
REGISTRY[GalleryDLAdapter.tool_id] = GalleryDLAdapter
REGISTRY[GitHubDeveloperIdentityAdapter.tool_id] = GitHubDeveloperIdentityAdapter
REGISTRY[OpenAlexScholarAdapter.tool_id] = OpenAlexScholarAdapter
REGISTRY[PhoneOfflineAdapter.tool_id] = PhoneOfflineAdapter
REGISTRY[PhonePresenceAdapter.tool_id] = PhonePresenceAdapter
REGISTRY[GravatarPublicAdapter.tool_id] = GravatarPublicAdapter
REGISTRY[GitHubDiscoveryAdapter.tool_id] = GitHubDiscoveryAdapter
REGISTRY[PGPPublicAdapter.tool_id] = PGPPublicAdapter
REGISTRY[OTXHistoricalURLAdapter.tool_id] = OTXHistoricalURLAdapter
REGISTRY[LiveSiteIntelligenceAdapter.tool_id] = LiveSiteIntelligenceAdapter
REGISTRY[DomainRDAPAdapter.tool_id] = DomainRDAPAdapter
REGISTRY[DomainRelationshipAdapter.tool_id] = DomainRelationshipAdapter
REGISTRY[UsernamePublicProfilesAdapter.tool_id] = UsernamePublicProfilesAdapter

__all__ = ["REGISTRY", "get_adapter"]
