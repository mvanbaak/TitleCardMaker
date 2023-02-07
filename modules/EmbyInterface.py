from base64 import b64encode
from datetime import datetime
from typing import Union

from modules.Debug import log
from modules.EpisodeDataSource import EpisodeDataSource
from modules.EpisodeInfo import EpisodeInfo
import modules.global_objects as global_objects
from modules.MediaServer import MediaServer
from modules.SeriesInfo import SeriesInfo
from modules.WebInterface import WebInterface

SourceImage = Union[str, None]

class EmbyInterface(EpisodeDataSource, MediaServer):
    """
    This class describes an interface to an Emby media server. This is a type
    of EpisodeDataSource (e.g. interface by which Episode data can be
    retrieved), as well as a MediaServer (e.g. a server in which cards can be
    loaded into).
    """

    """Filepath to the database of each episode's loaded card characteristics"""
    LOADED_DB = 'loaded_emby.json'

    """Series ID's that can be set by Emby"""
    SERIES_IDS = ('emby_id', 'imdb_id', 'tmdb_id', 'tvdb_id')

    """Datetime format string for airdates reported by Emby"""
    AIRDATE_FORMAT = '%Y-%m-%dT%H:%M:%S.%f000000Z'


    def __init__(self, url: str, api_key: str, verify_ssl: bool=True,
                 server_id: int=0) -> None:
        """
        Construct a new instance of an interface to an Emby server.

        Args:
            url: The API url communicating with Emby.
            api_key: The API key for API requests.
            verify_ssl: Whether to verify SSL requests.
            server_id: Server ID of this server.

        Raises:
            SystemExit: Invalid Sonarr URL/API key provided.
        """

        super().__init__()

        self.session = WebInterface('Emby', verify_ssl)
        self.info_set = global_objects.info_set
        self.url = url
        self.__params = {'api_key': api_key}
        self.server_id = server_id

        # Authenticate with server
        try:
            response = self.session._get(
                f'{self.url}/System/Info',
                params=self.__params
            )
            if not set(response).issuperset({'ServerName', 'Version', 'Id'}):
                raise Exception(f'Unable to authenticate with server')
        except Exception as e:
            log.critical(f'Cannot connect to Emby - returned error {e}')
            exit(1)

        # Get the ID's of all libraries within this server
        self.libraries = self._map_libraries()


    def _map_libraries(self) -> dict[str, tuple[int]]:
        """
        Map the libraries on this interface's Emby server.

        Returns:
            Dictionary whose keys are the names of the libraries, and whose
            values are tuples of the folder ID's for those libraries.
        """

        # Get all library folders 
        libraries = self.session._get(
            f'{self.url}/Library/SelectableMediaFolders',
            params=self.__params
        )

        # Parse each library name into tuples of parent ID's
        return {
            lib['Name']: tuple(int(folder['Id']) for folder in lib['SubFolders'])
            for lib in libraries
        }


    def set_series_ids(self, library_name: str, series_info: SeriesInfo) ->None:
        """
        Set the series ID's for the given SeriesInfo object.

        Args:
            library_name: The name of the library containing the series.
            series_info: Series to set the ID of.
        """

        # If all possible ID's are defined
        if series_info.has_ids(*self.SERIES_IDS):
            return None

        # If library not mapped, error and exit
        if (library_ids := self.libraries.get(library_name)) is None:
            log.error(f'Library "{library_name}" not found in Emby')
            return None

        # Generate provider ID query string
        ids = []
        if series_info.has_id('imdb_id'): ids += [f'imdb.{series_info.imdb_id}']
        if series_info.has_id('tmdb_id'): ids += [f'tmdb.{series_info.tmdb_id}']
        if series_info.has_id('tvdb_id'): ids += [f'tvdb.{series_info.tvdb_id}']
        provider_id_str = ','.join(ids)

        # Base params for all requests
        params = {
            'Recursive': True,
            'Years': series_info.year,
            'IncludeItemTypes': 'series',
            'SearchTerm': series_info.name,
            'Fields': 'ProviderIds',
        } | self.__params \
          |({'AnyProviderIdEquals': provider_id_str} if provider_id_str else {})

        # Look for this series in each library subfolder
        for parent_id in library_ids:
            response = self.session._get(
                f'{self.url}/Items',
                params=params | {'ParentId': parent_id}
            )

            # If no responses, skip
            if response['TotalRecordCount'] == 0: continue

            # Go through all items and match name and type, setting database IDs
            for result in response['Items']:
                if (result['Type'] == 'Series'
                    and series_info.matches(result['Name'])):
                    # Set Emby, IMDb, TMDb, or TVDb
                    emby_id = f'{self.server_id}-{result["Id"]}'
                    self.info_set.set_emby_id(series_info, emby_id)
                    if (imdb_id := result['ProviderIds'].get('IMDB')):
                        self.info_set.set_imdb_id(series_info, imdb_id)
                    if (tmdb_id := result['ProviderIds'].get('Tmdb')):
                        self.info_set.set_tmdb_id(series_info, int(tmdb_id))
                    if (tvdb_id := result['ProviderIds'].get('Tvdb')):
                        self.info_set.set_tvdb_id(series_info, int(tvdb_id))
                        
                    return None

        # Not found on server
        log.warning(f'Series "{series_info}" was not found under library '
                    f'"{library_name}" in Emby')
        return None 


    def set_episode_ids(self, series_info: SeriesInfo,
                        infos: list[EpisodeInfo]) -> None:
        """
        Set the Episode ID's for the given EpisodeInfo objects.

        Args:
            series_info: Series to get the episodes of.
            infos: List of EpisodeInfo objects to set the ID's of.
        """

        self.get_all_episodes(series_info)


    def get_all_episodes(self, series_info: SeriesInfo) -> list[EpisodeInfo]:
        """
        Gets all episode info for the given series. Only episodes that have 
        already aired are returned.

        Args:
            series_info: Series to get the episodes of.

        Returns:
            List of EpisodeInfo objects for this series.
        """

        # If series has no Emby ID, cannot query episodes
        if not series_info.has_id('emby_id'):
            log.warning(f'Series not found in Emby {series_info!r}')
            return []

        # Get all episodes for this series from Emby
        emby_id = series_info.emby_id.split('-')[1]
        params = {
            'Recursive': True,
            'ParentId': emby_id,
            'IncludeItemTypes': 'episode',
            'Fields': 'ProviderIds',
        } | self.__params

        response = self.session._get(
            f'{self.url}/Shows/{emby_id}/Episodes',
            params=params
        )

        # Parse each returned episode into EpisodeInfo object
        all_episodes = []
        for episode in response['Items']:
            # Parse airdate for this episode
            airdate=None
            try:
                airdate = datetime.strptime(episode['PremiereDate'],
                                            self.AIRDATE_FORMAT)
            except Exception as e:
                log.exception(f'Cannot parse airdate', e)
                log.debug(f'Episode data: {episode}')

            episode_info = self.info_set.get_episode_info(
                series_info,
                episode['Name'],
                episode['ParentIndexNumber'],
                episode['IndexNumber'],
                emby_id=f'{self.server_id}-{episode.get("Id")}',
                imdb_id=episode['ProviderIds'].get('Imdb'),
                tmdb_id=episode['ProviderIds'].get('Tmdb'),
                tvdb_id=episode['ProviderIds'].get('Tvdb'),
                tvrage_id=episode['ProviderIds'].get('TvRage'),
                airdate=airdate,
                title_match=True,
                queried_emby=True,
            )

            # Add to list
            if episode_info is not None:
                all_episodes.append(episode_info)

        return all_episodes


    def set_title_cards(self, library_name: str, series_info: 'SeriesInfo',
                        episode_map: dict[str, 'Episode']) -> None:
        """
        Set the title cards for the given series. This only updates episodes
        that have title cards, and those episodes whose card filesizes are
        different than what has been set previously.

        Args:
            series_info: The series to update.
            episode_map: Dictionary of episode keys to Episode objects to update
                the cards of.
        """

        # If series has no Emby ID, cannot set title cards
        if not series_info.has_id('emby_id'):
            return None

        # Filter loaded cards
        filtered_episodes = self._filter_loaded_cards(
            library_name, series_info, episode_map
        )

        # If no episodes remain, exit
        if len(filtered_episodes) == 0:
            return None

        # Go through each remaining episode and load the card
        loaded_count = 0
        for episode in filtered_episodes.values():
            # Skip episodes without Emby ID's (e.g. not in Emby)
            if (emby_id := episode.episode_info.emby_id) is None:
                continue

            # Image content must be Base64-encoded
            card_base64 = b64encode(episode.destination.read_bytes())

            # Submit POST request for image upload
            try:
                emby_id = episode.episode_info.emby_id.split('-')[1]
                self.session.session.post(
                    url=f'{self.url}/Items/{emby_id}/Images/Primary',
                    headers={'Content-Type': 'image/jpeg'},
                    params=self.__params,
                    data=card_base64,
                )
                loaded_count += 1
            except Exception as e:
                log.exception(f'Unable to upload {episode.destination.resolve()}'
                              f' to {series_info}', e)
                continue

            # Update loaded database for this episode
            self.loaded_db.upsert({
                'library': library_name,
                'series': series_info.full_name,
                'season': episode.episode_info.season_number,
                'episode': episode.episode_info.episode_number,
                'filesize': episode.destination.stat().st_size,
                'spoiler': episode.spoil_type,
            }, self._get_condition(library_name, series_info, episode))

        # Log load operations to user
        if loaded_count > 0:
            log.info(f'Loaded {loaded_count} cards for "{series_info}"')


    def get_source_image(self) -> SourceImage:
        raise NotImplementedError(f'All EpisodeDataSources must implement this')