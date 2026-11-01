"""Exact, ordered browser-message evidence; never infer success from OCR similarity."""
from dataclasses import dataclass
from .vision import normalize

MESSAGE_SCRIPT = r'''
function chatMessages(){
  const containers=document.querySelectorAll('#message-list-scrollable');
  if(containers.length!==1)throw new Error('Chat container missing or ambiguous');
  const container=containers[0];
  const lists=container.querySelectorAll('#msg-list-container .ant-list-items');
  if(lists.length!==1)throw new Error('Message list is not unique');
  const messages=[];
  const hasPrefix=(e,p)=>Array.from(e.classList).some(c=>c.startsWith(p));
  const descendants=(e,p)=>Array.from(e.querySelectorAll('*')).filter(n=>hasPrefix(n,p));
  for(const wrapper of lists[0].children){
    const rows=descendants(wrapper,'message-row--');
    if(!rows.length){
      const text=(wrapper.innerText||'').trim();
      if(!text || /^(?:(?:今天|昨天|前天)\s*)?(?:\d{2,4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\s*)?\d{1,2}:\d{2}$/.test(text))continue;
      throw new Error('Unrecognized message row');
    }
    if(rows.length!==1)throw new Error('Message row is not unique');
    const row=rows[0], contents=descendants(row,'message-content--'), avatars=descendants(row,'avatar--');
    if(contents.length!==1 || avatars.length!==1)throw new Error('Message sender structure is uncertain');
    const content=contents[0], avatar=avatars[0];
    const role=(content.compareDocumentPosition(avatar)&Node.DOCUMENT_POSITION_FOLLOWING)?'seller':'buyer';
    const texts=descendants(content,'message-text--');
    if(texts.length>1)throw new Error('Message text is not unique');
    const copy=content.cloneNode(true);
    for(const status of descendants(copy,'read-status-text--'))status.remove();
    const media=Array.from(content.querySelectorAll('img,video,audio')).map(n=>({kind:n.tagName.toLowerCase(),src:n.currentSrc||n.src||'',alt:n.alt||''}));
    let type='text', text;
    if(texts.length===1 && media.length===0){
      const bubble=texts[0];
      if(role==='seller'&&!hasPrefix(bubble,'message-text-right--'))throw new Error('Seller direction mismatch');
      if(role==='buyer'&&!hasPrefix(bubble,'message-text-left--'))throw new Error('Buyer direction mismatch');
      text=bubble.innerText;
    }else{
      text=(copy.innerText||'').trim();
      type=media.length ? (media.some(n=>n.kind==='video')?'video':media.some(n=>n.kind==='audio')?'audio':'image') : 'card';
      if(!text && !media.length)throw new Error('Empty or unsupported message');
    }
    messages.push({role,type,text:text||'',media});
  }
  if(!messages.length)throw new Error('No browser messages');
  return JSON.stringify({messages,position:JSON.parse(chatScrollState())});
}
'''

@dataclass(frozen=True)
class MessageState:
    customer: str
    messages: tuple
    position: dict

def parse_messages(raw):
    if not isinstance(raw,list) or not raw or len(raw)>10000:
        raise ValueError('网页消息列表为空或结构异常。')
    messages=[]
    for item in raw:
        if not isinstance(item,dict) or item.get('role') not in ('buyer','seller','system'):
            raise ValueError('网页消息发送者不确定。')
        if item.get('type') not in ('text','card','image','audio','video','file','emoji') or not isinstance(item.get('text'),str):
            raise ValueError('网页消息类型或正文不确定。')
        media=item.get('media',[])
        if not isinstance(media,list) or any(not isinstance(m,dict) or any(not isinstance(m.get(k,''),str) for k in ('kind','src','alt')) for m in media):
            raise ValueError('网页媒体信息结构异常。')
        text=normalize(item['text'])
        if not text and not media:raise ValueError('网页消息正文为空。')
        messages.append({'role':item['role'],'type':item['type'],'text':text,
                         'media':[{k:m.get(k,'') for k in ('kind','src','alt')} for m in media]})
    return tuple(messages)

def compare_reply(before: MessageState, after: MessageState, text):
    if before.customer!=after.customer:return False,'customer_changed',()
    old,new=before.messages,after.messages
    if len(new)<len(old) or new[:len(old)]!=old:return False,'history_changed',()
    added=new[len(old):]
    if not added:return False,'no_new_message',()
    sellers=[m for m in added if m['role']=='seller']
    meaningful=[m for m in added if m['role']!='system']
    if not meaningful or meaningful[0]['role']!='seller':return False,'unexpected_message_before_reply',added
    if len(sellers)!=1:return False,'seller_reply_not_unique',added
    if sellers[0]['type']!='text' or sellers[0]['text']!=normalize(text):return False,'reply_text_mismatch',added
    return True,'new_seller_reply_verified',added
