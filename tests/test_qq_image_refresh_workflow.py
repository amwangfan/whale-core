"""Expired QQ image keys can refresh without losing the incoming picture source."""
from types import SimpleNamespace

from whale_core.core.media_input import MediaInputError
from whale_core.life.channels import OwnerEvent
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.memory.store import InMemoryStore


class Brain:
    def reply(self, text, **kwargs):
        self.seen_images = kwargs.get('images', ())
        return '收到图片'


def event(*attachments):
    return OwnerEvent(owner_id='owner', channel='qq', text='看看这张图',
                      conversation='private:owner', message_id='pic-one', attachments=attachments)


def test_missing_key_refreshes_once_and_passes_real_bytes_to_companion():
    ready = []
    refreshes = []
    downloaded = []
    def refresh():
        refreshes.append(True)
        ready.append(True)
        return True
    class Validator:
        def from_url(self, url):
            downloaded.append(url)
            return SimpleNamespace(data=b'validated-image', mime='image/png')
    brain = Brain()
    workflow = QqInboundWorkflow(
        InMemoryStore(), brain, media_validator=Validator(),
        media_url_resolver=lambda att: ('https://media.example/current',) if ready else (),
        media_refresh=refresh)
    images = workflow._attachment_images(event({'kind':'pic', 'file_token':'opaque'}))
    workflow.companion.chat('看看这张图', images=images)
    assert len(refreshes) == 1 and len(downloaded) == 1
    assert brain.seen_images == ((b'validated-image','image/png'),)


def test_expired_saved_url_is_rebuilt_after_refresh():
    ready = []
    seen = []
    class Validator:
        def from_url(self, url):
            seen.append(url)
            if url.endswith('expired'):
                raise MediaInputError('download failed')
            return SimpleNamespace(data=b'image',mime='image/jpeg')
    workflow = QqInboundWorkflow(
        InMemoryStore(), Brain(), media_validator=Validator(),
        media_url_resolver=lambda _: ('https://media.example/fresh',) if ready else (),
        media_refresh=lambda: ready.append(True) or True)
    assert workflow._attachment_images(event({'kind':'pic','original_url':'/download',
        'url':'https://media.example/expired'})) == ((b'image','image/jpeg'),)
    assert seen == ['https://media.example/expired','https://media.example/fresh']


def test_failed_refresh_is_once_per_event_and_does_not_invent_an_image():
    refreshes=[]
    workflow=QqInboundWorkflow(InMemoryStore(),Brain(),media_url_resolver=lambda _: (),
        media_refresh=lambda: refreshes.append(True) or False)
    assert workflow._attachment_images(event({'kind':'pic','file_token':'one'},
        {'kind':'pic','file_token':'two'})) == ()
    assert refreshes == [True]


def test_existing_single_image_index_avoids_redownload():
    store=InMemoryStore()
    store.put('msgimg:pic-one', {'artifact':'a','mime':'image/png'})
    workflow=QqInboundWorkflow(store,Brain(),media_url_resolver=lambda _: (_ for _ in ()).throw(AssertionError()))
    workflow._artifact_reader=lambda aid: (SimpleNamespace(content_type='image/png'),b'already-validated')
    assert workflow._attachment_images(event({'kind':'pic'})) == ((b'already-validated','image/png'),)


def test_multiple_attachments_do_not_collapse_to_first_persisted_image():
    store=InMemoryStore()
    store.put('msgimg:pic-one', {'artifact':'first','mime':'image/png'})
    class Validator:
        def from_url(self,url):return SimpleNamespace(data=url.encode(),mime='image/png')
    workflow=QqInboundWorkflow(store,Brain(),media_validator=Validator())
    workflow._artifact_reader=lambda _: (SimpleNamespace(content_type='image/png'),b'first-only')
    images=workflow._attachment_images(event({'kind':'pic','url':'https://media.example/one'},
        {'kind':'pic','url':'https://media.example/two'}))
    assert len(images)==2 and images[1][0]==b'https://media.example/two'


def test_sse_rkey_gap_to_companion_image_bytes(monkeypatch):
    from weq_channel import wq_push
    keys=[]
    monkeypatch.setattr(wq_push,'_load_weq_rkeys',lambda: keys)
    incoming=wq_push.sse_message_to_incoming({
        'type':'message','senderUin':'person','msgId':'image-gap','chatType':'c2c',
        'convId':'owner','text':'看这个','elements':[{'type':'pic','data':{
            'originalUrl':'/download?appid=1406&fileid=synthetic&spec=0',
            'fileToken':'synthetic','imgWidth':10,'imgHeight':10}}]},self_uin='bot')
    assert incoming.attachments[0]['urls']==()
    class Validator:
        def from_url(self,url):
            assert 'rkey=NEW' in url
            return SimpleNamespace(data=b'fresh-picture',mime='image/jpeg')
    def refresh():
        keys.append(('1406','&rkey=NEW'))
        return True
    brain=Brain()
    workflow=QqInboundWorkflow(InMemoryStore(),brain,media_validator=Validator(),
        media_url_resolver=wq_push.resolve_pic_attachment_urls,media_refresh=refresh)
    images=workflow._attachment_images(event(*incoming.attachments))
    workflow.companion.chat('看这个',images=images)
    assert brain.seen_images==((b'fresh-picture','image/jpeg'),)
