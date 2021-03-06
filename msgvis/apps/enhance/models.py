from django.db import models
from django.conf import settings
import textblob

from fields import PositiveBigIntegerField
from msgvis.apps.corpus.models import Message, Dataset
from msgvis.apps.base import models as base_models
from msgvis.apps.corpus import utils

# Create your models here.



# import the logging library
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)


class Dictionary(models.Model):
    name = models.CharField(max_length=100)
    dataset = models.ForeignKey(Dataset, related_name="dictionary", null=True, blank=True, default=None)
    settings = models.TextField()

    time = models.DateTimeField(auto_now_add=True)

    num_docs = PositiveBigIntegerField(default=0)
    num_pos = PositiveBigIntegerField(default=0)
    num_nnz = PositiveBigIntegerField(default=0)

    @property
    def gensim_dictionary(self):
        if not hasattr(self, '_gensim_dict'):
            setattr(self, '_gensim_dict', self._make_gensim_dictionary())
        return getattr(self, '_gensim_dict')

    def get_word_id(self, bow_index):
        if not hasattr(self, '_index2id'):
            g = self.gensim_dictionary
        try:
            return self._index2id[bow_index]
        except KeyError:
            return None

    def _make_gensim_dictionary(self):

        logger.info("Building gensim dictionary from database")

        setattr(self, '_index2id', {})

        from gensim import corpora

        gensim_dict = corpora.Dictionary()
        gensim_dict.num_docs = self.num_docs
        gensim_dict.num_pos = self.num_pos
        gensim_dict.num_nnz = self.num_nnz

        for word in self.words.all():
            self._index2id[word.index] = word.id
            gensim_dict.token2id[word.text] = word.index
            gensim_dict.dfs[word.index] = word.document_frequency

        logger.info("Dictionary contains %d words" % len(gensim_dict.token2id))

        return gensim_dict

    def _populate_from_gensim_dictionary(self, gensim_dict):

        self.num_docs = gensim_dict.num_docs
        self.num_pos = gensim_dict.num_pos
        self.num_nnz = gensim_dict.num_nnz
        self.save()

        logger.info("Saving gensim dictionary '%s' in the database" % self.name)

        batch = []
        count = 0
        print_freq = 10000
        batch_size = 1000
        total_words = len(gensim_dict.token2id)

        for token, id in gensim_dict.token2id.iteritems():
            word = Word(dictionary=self,
                        text=token,
                        index=id,
                        document_frequency=gensim_dict.dfs[id])
            batch.append(word)
            count += 1

            if len(batch) > batch_size:
                Word.objects.bulk_create(batch)
                batch = []

                if settings.DEBUG:
                    # prevent memory leaks
                    from django.db import connection

                    connection.queries = []

            if count % print_freq == 0:
                logger.info("Saved %d / %d words in the database dictionary" % (count, total_words))

        if len(batch):
            Word.objects.bulk_create(batch)
            count += len(batch)

            logger.info("Saved %d / %d words in the database dictionary" % (count, total_words))

        return self

    @classmethod
    def _create_from_texts(cls, tokenized_texts, name, dataset, settings, minimum_frequency=2):
        from gensim.corpora import Dictionary as GensimDictionary

        # build a dictionary
        logger.info("Building a dictionary from texts")
        dictionary = GensimDictionary(tokenized_texts)

        # Remove extremely rare words
        logger.info("Dictionary contains %d words. Filtering..." % len(dictionary.token2id))
        dictionary.filter_extremes(no_below=minimum_frequency, no_above=1, keep_n=None)
        dictionary.compactify()
        logger.info("Dictionary contains %d words." % len(dictionary.token2id))

        dict_model = cls(name=name,
                         dataset=dataset,
                         settings=settings)
        dict_model.save()

        dict_model._populate_from_gensim_dictionary(dictionary)

        return dict_model

    def _vectorize_corpus(self, queryset, tokenizer):

        import math

        logger.info("Saving document word vectors in corpus.")

        total_documents = self.num_docs
        gdict = self.gensim_dictionary
        count = 0
        total_count = queryset.count()
        batch = []
        batch_size = 1000
        print_freq = 10000

        for msg in queryset.iterator():
            text = msg.text
            bow = gdict.doc2bow(tokenizer.tokenize(text))

            for word_index, word_freq in bow:
                word_id = self.get_word_id(word_index)
                document_freq = gdict.dfs[word_index]

                # Not sure why tf is calculated like the final version
                # num_tokens = len(gdict)
                # tf = float(word_freq) / float(num_tokens)
                # idf = math.log(total_documents / document_freq)
                # tfidf = tf * idf
                tfidf = word_freq * math.log(total_documents / document_freq)
                batch.append(MessageWord(dictionary=self,
                                         word_id=word_id,
                                         word_index=word_index,
                                         count=word_freq,
                                         tfidf=tfidf,
                                         message=msg))
            count += 1

            if len(batch) > batch_size:
                MessageWord.objects.bulk_create(batch)
                batch = []

                if settings.DEBUG:
                    # prevent memory leaks
                    from django.db import connection

                    connection.queries = []

            if count % print_freq == 0:
                logger.info("Saved word-vectors for %d / %d documents" % (count, total_count))

        if len(batch):
            MessageWord.objects.bulk_create(batch)
            logger.info("Saved word-vectors for %d / %d documents" % (count, total_count))

        logger.info("Created %d word vector entries" % count)


    def _build_lda(self, name, corpus, num_topics=30, words_to_save=200, multicore=True):
        from gensim.models import LdaMulticore, LdaModel

        gdict = self.gensim_dictionary

        if multicore:
            lda = LdaMulticore(corpus=corpus,
                               num_topics=num_topics,
                               workers=3,
                               id2word=gdict)
        else:
            lda = LdaModel(corpus=corpus,
                               num_topics=num_topics,
                               id2word=gdict)

        model = TopicModel(name=name, dictionary=self)
        model.save()

        topics = []
        for i in range(num_topics):
            topic = lda.show_topic(i, topn=words_to_save)
            alpha = lda.alpha[i]

            topicm = Topic(model=model, name="?", alpha=alpha, index=i)
            topicm.save()
            topics.append(topicm)

            words = []
            for prob, word_text in topic:
                word_index = gdict.token2id[word_text]
                word_id = self.get_word_id(word_index)
                tw = TopicWord(topic=topicm,
                               word_id=word_id, word_index=word_index,
                               probability=prob)
                words.append(tw)
            TopicWord.objects.bulk_create(words)

            most_likely_word_scores = topicm.word_scores\
                .order_by('-probability')\
                .prefetch_related('word')
                
            topicm.name = ', '.join([score.word.text for score in most_likely_word_scores[:3]])
            topicm.save()

            if settings.DEBUG:
                # prevent memory leaks
                from django.db import connection

                connection.queries = []

        model.save_to_file(lda)

        return (model, lda)

    def _apply_lda(self, model, corpus, lda=None):

        if lda is None:
            # recover the lda
            lda = model.load_from_file()

        total_documents = len(corpus)
        count = 0
        batch = []
        batch_size = 1000
        print_freq = 10000

        topics = list(model.topics.order_by('index'))

        # Go through the bows and get their topic mixtures
        for bow in corpus:
            mixture = lda[bow]
            message_id = corpus.current_message_id

            for topic_index, prob in mixture:
                topic = topics[topic_index]
                itemtopic = MessageTopic(topic_model=model,
                                         topic=topic,
                                         message_id=message_id,
                                         probability=prob)
                batch.append(itemtopic)

            count += 1

            if len(batch) > batch_size:
                MessageTopic.objects.bulk_create(batch)
                batch = []

                if settings.DEBUG:
                    # prevent memory leaks
                    from django.db import connection

                    connection.queries = []

            if count % print_freq == 0:
                logger.info("Saved topic-vectors for %d / %d documents" % (count, total_documents))

        if len(batch):
            MessageTopic.objects.bulk_create(batch)
            logger.info("Saved topic-vectors for %d / %d documents" % (count, total_documents))

    def _evaluate_lda(self, model, corpus, lda=None):

        if lda is None:
            # recover the lda
            lda = model.load_from_file()

        logger.info("Calculating model perplexity on entire corpus...")
        model.perplexity = lda.log_perplexity(corpus)
        logger.info("Perplexity: %f" % model.perplexity)
        model.save()


class Word(models.Model):
    dictionary = models.ForeignKey(Dictionary, related_name='words')
    index = models.IntegerField()
    text = base_models.Utf8CharField(max_length=100)
    document_frequency = models.IntegerField()

    messages = models.ManyToManyField(Message, through='MessageWord', related_name='words')

    def __repr__(self):
        return self.text

    def __unicode__(self):
        return self.__repr__()


class TopicModel(models.Model):
    dictionary = models.ForeignKey(Dictionary)

    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200)

    time = models.DateTimeField(auto_now_add=True)
    perplexity = models.FloatField(default=0)

    def load_from_file(self):
        from gensim.models import LdaMulticore

        return LdaMulticore.load("lda_out_%d.model" % self.id)

    def save_to_file(self, gensim_lda):
        gensim_lda.save("lda_out_%d.model" % self.id)

    def get_probable_topic(self, message):
        """For this model, get the most likely topic for the message."""
        message_topics = message.topic_probabilities\
            .filter(topic_model=self)\
            .only('topic', 'probability')

        max_prob = -100000
        probable_topic = None
        for mt in message_topics:
            if mt.probability > max_prob:
                probable_topic = mt.topic
                max_prob = mt.probability

        return probable_topic


class Topic(models.Model):
    model = models.ForeignKey(TopicModel, related_name='topics')
    name = base_models.Utf8CharField(max_length=100)
    description = base_models.Utf8CharField(max_length=200)
    index = models.IntegerField()
    alpha = models.FloatField()

    messages = models.ManyToManyField(Message, through='MessageTopic', related_name='topics')
    words = models.ManyToManyField(Word, through='TopicWord', related_name='topics')


class TopicWord(models.Model):
    word = models.ForeignKey(Word, related_name='topic_scores')
    topic = models.ForeignKey(Topic, related_name='word_scores')

    word_index = models.IntegerField()
    probability = models.FloatField()


class MessageWord(models.Model):
    class Meta:
        index_together = (
            ('dictionary', 'message'),
            ('message', 'word'),
        )

    dictionary = models.ForeignKey(Dictionary, db_index=False)

    word = models.ForeignKey(Word, related_name="message_scores")
    message = models.ForeignKey(Message, related_name='word_scores', db_index=False)

    word_index = models.IntegerField()
    count = models.FloatField()
    tfidf = models.FloatField()


class MessageTopic(models.Model):
    class Meta:
        index_together = (
            ('topic_model', 'message'),
            ('message', 'topic'),
        )

    topic_model = models.ForeignKey(TopicModel, db_index=False)

    topic = models.ForeignKey(Topic, related_name='message_probabilities')
    message = models.ForeignKey(Message, related_name="topic_probabilities", db_index=False)

    probability = models.FloatField()


    @classmethod
    def get_examples(cls, topic):
        examples = cls.objects.filter(topic=topic)
        return examples.order_by('-probability')


def set_message_sentiment(message, save=True):
    message.sentiment = int(round(textblob.TextBlob(message.text).sentiment.polarity))
    if save:
        message.save()

class TweetWord(models.Model):
    dataset = models.ForeignKey(Dataset, related_name="tweet_words", null=True, blank=True, default=None)
    original_text = base_models.Utf8CharField(max_length=100, db_index=True, blank=True, default="")
    pos = models.CharField(max_length=4, null=True, blank=True, default="")
    text = base_models.Utf8CharField(max_length=100, db_index=True, blank=True, default="")
    messages = models.ManyToManyField(Message, related_name='tweet_words')

    def __repr__(self):
        return self.text

    def __unicode__(self):
        return self.__repr__()

    @property
    def related_words(self):
        return TweetWord.objects.filter(dataset=self.dataset, text=self.text).all()

    @property
    def all_messages(self):
        queryset = self.dataset.message_set.all()
        queryset = queryset.filter(utils.levels_or("tweet_words__id", map(lambda x: x.id, self.related_words)))
        return queryset



class PrecalcCategoricalDistribution(models.Model):
    dataset = models.ForeignKey(Dataset, related_name="distributions", null=True, blank=True, default=None)
    dimension_key = models.CharField(db_index=True, max_length=64, blank=True, default="")
    level = base_models.Utf8CharField(db_index=True, max_length=128, blank=True, default="")
    count = models.IntegerField()

    class Meta:
        index_together = [
            ["dimension_key", "level"],
        ]