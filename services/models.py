from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Any, Dict, Type

@dataclass
class AlbumCandidate:
    """
    Represents a potential album match from a search operation on a service.

    This class holds information about a realease that has been identified as a
    possible match for a user's search query. It typically contains enough
    information to display to the user and to be used for subsequent operations
    like listing potential images for this release.

    Attributes:
        identifier (Any): A unique identifier for this album candidate within its source service.
                          This is used to fetch more details or images later.
        album_name (Optional[str]): The name of the album.
        artist_name (Optional[str]): The name of the artist associated with this album.
        source_service (str): The name of the service from which this candidate was retrieved.
        extra_data (Dict[str, Any]): A dictionary for any additional service-specific data
                                     that might be useful for later processing or display.
    """
    identifier: Any
    album_name: Optional[str] = None
    artist_name: Optional[str] = None
    source_service: str = ""
    extra_data: Dict[str, Any] = field(default_factory=dict)

@dataclass
class PotentialImage:
    """
    Represents an image URL that has been identified for an AlbumCandidate,
    but for which full details (like dimensions or exact file type) haven't
    necessarily been fetched yet.

    This class is typically used as an intermediate step between finding an album
    and resolving the full details of its associated images.

    Attributes:
        identifier (Any): A unique identifier for this potential image within its source service
                          or context (e.g., its full URL).
        thumbnail_url (str): URL to a smaller version of the image, suitable for previews.
        full_image_url (str): URL to the full-resolution version of the image.
        source_candidate (AlbumCandidate): The AlbumCandidate this image belongs to.
        original_type (Optional[str]): A string describing the type of the image as provided
                                       by the source service (e.g., "Primary", "Cover", "Front").
        extra_data (Dict[str, Any]): A dictionary for any additional service-specific data
                                     about the image.
        is_front (bool): Indicates if this image is likely a front cover. Defaults to True.
    """
    identifier: Any
    thumbnail_url: str
    full_image_url: str
    source_candidate: AlbumCandidate
    original_type: Optional[str] = None
    extra_data: Dict[str, Any] = field(default_factory=dict)
    is_front: bool = True

@dataclass
class ImageResult:
    """
    Represents a fully resolved image, including its dimensions and source.

    This class is the final representation of an image that has been selected
    and for which all necessary details have been fetched. It contains enough
    information to display the image, its thumbnail, and relevant metadata.

    Attributes:
        thumbnail_url (str): URL to a smaller version of the image, suitable for previews.
        full_image_url (str): URL to the full-resolution version of the image.
        full_width (int): The width of the full-resolution image in pixels.
        full_height (int): The height of the full-resolution image in pixels.
        source_candidate (AlbumCandidate): The AlbumCandidate from which this image was ultimately sourced.
        thumbnail_data (Optional[bytes]): Optional. If the retriever pre-fetched thumbnail
                                          binary data, it can be stored here.
        original_type (Optional[str]): A string describing the type of the image as provided
                                       by the source service (e.g., "Primary", "Cover", "Front",
                                       "Screenshot"). This is service-specific.
        source_potential_image_identifier (Optional[Any]): The identifier of the PotentialImage
                                                           from which this ImageResult was resolved.
                                                           Useful for tracing or debugging.
        is_front (bool): Indicates if this image is considered a front cover. Defaults to True.
    """
    thumbnail_url: str
    full_image_url: str
    full_width: int
    full_height: int
    source_candidate: AlbumCandidate
    thumbnail_data: Optional[bytes] = None # Optional: if thumbnail data is pre-fetched by retriever
    original_type: Optional[str] = None
    source_potential_image_identifier: Optional[Any] = None
    is_front: bool = field(default=True)

    @property
    def source_service(self) -> str:
        """The name of the service from which this image was ultimately sourced."""
        return self.source_candidate.source_service

    @property
    def album_name(self) -> Optional[str]:
        """The name of the album this image is associated with."""
        return self.source_candidate.album_name

    @property
    def artist_name(self) -> Optional[str]:
        """The name of the artist associated with the album of this image."""
        return self.source_candidate.artist_name

    @classmethod
    def from_potential_image(
        cls: Type['ImageResult'],
        potential_image: PotentialImage,
        full_width: int,
        full_height: int,
        **overrides: Any
    ) -> 'ImageResult':
        """
        Constructs an ImageResult from a PotentialImage instance.

        Args:
            potential_image: The PotentialImage to base the ImageResult on.
            full_width: The width of the full-resolution image. This is a
                        mandatory argument for this constructor method.
            full_height: The height of the full-resolution image. This is a
                         mandatory argument for this constructor method.
            **overrides: Keyword arguments to override any fields of the ImageResult.
                         These take precedence over data from potential_image.

        Returns:
            An instance of ImageResult.
        """
        init_data = {
            'thumbnail_url': potential_image.thumbnail_url,
            'full_image_url': potential_image.full_image_url,
            'source_candidate': potential_image.source_candidate,
            'thumbnail_data': None, # Default, can be overridden
            'original_type': potential_image.original_type,
            'source_potential_image_identifier': potential_image.identifier,
            'is_front': potential_image.is_front,
        }

        init_data['full_width'] = full_width
        init_data['full_height'] = full_height

        init_data.update(overrides)

        return cls(**init_data)
