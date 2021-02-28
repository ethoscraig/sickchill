import datetime
import json
import logging
import os
import threading
import traceback

import imdb
import tmdbsimple
from imdb.parser.http.piculet import Path, Rule
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy_utils import get_mapper
from tmdbsimple import movies, search

from .. import settings
from ..oldbeard.db import db_cons, db_full_path, db_locks
from . import models

logger = logging.getLogger('sickchill.movies')


class MoviesList:
    def __init__(self):
        tmdbsimple.API_KEY = settings.TMDB_API_KEY

        self.filename = "movies.db"
        self.full_path = db_full_path(self.filename)

        if self.filename not in db_cons or not db_cons[self.filename]:

            load_defaults = not os.path.exists(self.full_path)

            models.Session.configure(
                bind=create_engine(
                    f"sqlite:///{self.full_path}",
                    echo=settings.DBDEBUG,
                    connect_args={"check_same_thread": False},
                    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False)
                )
            )
            self.session: Session = models.Session()

            models.Base.metadata.create_all(self.session.bind, checkfirst=True)

            if load_defaults:
                for table in models.Base.metadata.sorted_tables:
                    try:
                        model = get_mapper(table)
                        if hasattr(model.class_, 'default_data'):
                            model.class_.default_data(target=table, session=self.session)
                    except ValueError:
                        # A relationship table has no model
                        pass
                    except Exception as e:
                        logger.info('Error on loading defaults for table %s', table)
                        logger.debug(traceback.format_exc())
                        raise e

                self.session.flush()
                self.session.commit()

            db_locks[self.filename] = threading.Lock()
            db_cons[self.filename] = self.session
        else:
            self.session: Session = db_cons[self.filename]

        self.imdb = imdb.IMDb()
        try:
            self.imdb.topBottomProxy.moviemeter100_parser.rules[0].extractor.rules.append(
                Rule(
                    key='cover url',
                    extractor=Path('./td[@class="posterColumn"]/a/img/@src')
                )
            )
        except Exception:
            pass

    def __iter__(self):
        for item in self.query.all():
            yield item

    def __getitem__(self, pk):
        return self.query.get(pk)

    def __contains__(self, pk):
        try:
            self.__getitem__(pk)
            return True
        except KeyError:
            return False

    @staticmethod
    def search_tmdb(query=None, tmdb_id=None, year=None, language=None, adult=False):
        if tmdb_id:
            results = [movies.Movies(id=tmdb_id)]
        elif query:
            tmdb_kwargs = dict(query=query, year=year, language=language, adult=adult)
            tmdb_kwargs = {key: value for key, value in tmdb_kwargs.items() if value}
            results = search.Search().movie(**tmdb_kwargs)['results']
        else:
            raise Exception('Query or tmdb id is required!')

        return results

    @staticmethod
    def popular_tmdb(language=None):
        tmdb_kwargs = dict(language=language) if language else dict()
        return movies.Movies().popular(**tmdb_kwargs)['results']

    def search_imdb(self, query: str = ''):
        return self.imdb.search_movie(title=query)

    def popular_imdb(self):
        return self.imdb.get_popular100_movies()

    def add_from_tmdb(self, tmdb_id: str, language: str = settings.INDEXER_DEFAULT_LANGUAGE):
        logger.debug(f'Adding movie from tmdb with id: {tmdb_id}')
        existing = self.session.query(models.IndexerData).filter_by(code=tmdb_id).first()
        if existing:
            logger.debug(f'Movie already existed as {existing.models.name}')
            return existing.movie

        tmdb_object = tmdbsimple.movies.Movies(id=tmdb_id).info()
        return self.add_from_imdb(tmdb_object['imdb_id'], language, tmdb_primary=True)

    def add_from_imdb(self, imdb_id: str, language: str = settings.INDEXER_DEFAULT_LANGUAGE, tmdb_primary=False):
        imdb_id = imdb_id.strip('tt')
        if not tmdb_primary:
            logger.debug(f'Adding movie from imdb id: {imdb_id}')

        existing = self.session.query(models.IndexerData).filter_by(pk=imdb_id).first()
        if existing:
            logger.debug(f'Movie already existed as {existing.models.name}')
            return existing.movie

        imdb_object = self.imdb.get_movie(imdb_id)
        tmdb_id = tmdbsimple.find.Find(id=imdb_id).info(external_source='imdb_id')['movie_results'][0]['id']
        tmdb_object = tmdbsimple.movies.Movies(id=tmdb_id).info()

        if tmdb_primary:
            instance = models.Movies(tmdb_object['title'], year=tmdb_object['release_date'].split('-')[0])
            if imdb_object['title'] and not instance.name:
                instance.name = imdb_object['title']
        else:
            instance = models.Movies(imdb_object['title'], year=imdb_object['year'] or tmdb_object)
            if tmdb_object['release_date'] and not instance.year:
                instance.year = tmdb_object['release_date'].split('-')[0]
            if tmdb_object['title'] and not instance.name:
                instance.name = tmdb_object['title']

        if tmdb_object['release_date']:
            instance.date = datetime.datetime.strptime(tmdb_object['release_date'], '%Y-%m-%d').date()

        instance.language = tmdb_object['original_language'] or language

        tmdb_data = models.IndexerData(site='tmdb', data=tmdb_object, code=tmdb_id)
        imdb_data = models.IndexerData(site='imdb', data=imdb_object, code=int(imdb_id))

        imdb_genres = self.imdb.get_title_genres(imdb_id)['genres']

        def add_imdb_genres():
            for genre in imdb_genres:
                logger.debug(f'Adding imdb genre {genre}')
                imdb_data.genres.append(models.Genres(name=genre))
            instance.indexer_data.append(imdb_data)

        def add_tmdb_genres():
            for genre in tmdb_object['genres']:
                if genre['name'] not in imdb_genres:
                    logger.debug(f'Adding tmdb genre {genre["name"]}')
                    tmdb_data.genres.append(models.Genres(name=genre['name']))
            instance.indexer_data.append(tmdb_data)

        if tmdb_primary:
            add_tmdb_genres()
            add_imdb_genres()
        else:
            add_imdb_genres()
            add_tmdb_genres()

        self.commit(instance)

        logger.debug(f'Returning instance for {instance.name}')
        return instance

    def commit(self, instance=None):
        logger.debug('Committing')
        if instance:
            self.session.add(instance)
        self.session.flush()
        self.session.commit()

    def delete(self, pk):
        instance = self.query.get(pk)
        if instance:
            self.session.delete(instance)

    @property
    def query(self):
        return self.session.query(models.Movies)

    def by_slug(self, slug):
        return self.query.filter_by(slug=slug).first()

    def search_providers(self, movie_object: models.Movies):
        # We should only need to support backlog for movies manually
        # Movies should be returned in the existing RSS searches if we have movies in our list
        strings = movie_object.search_strings()
        for provider in settings.providerList:
            if provider.can_backlog and provider.backlog_enabled and provider.supports_movies:
                results = provider.search(strings)
                for item in results:
                    models.Results(result=item, provider=provider, movie=movie_object)

            # TODO: Check if we need to break out here and stop hitting providers if we found a good result

    def snatch_movie(self, result: models.Results):
        pass

    def search_thread(self):
        pass
