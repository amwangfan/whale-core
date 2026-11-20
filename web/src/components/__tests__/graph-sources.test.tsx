// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach, expect,it,vi} from 'vitest';
import {filterGraph,keywordGraph,memberGraph,parseMcp} from '../graphSources';
import GraphImport from '../GraphImport';
import KeywordDetail from '../KeywordDetail';
import {studioApi} from '../../api';

const a={type:'entity',name:'甲',entityType:'人物',observations:['整理星图']};
const b={type:'entity',name:'星图',entityType:'作品',observations:[]};
const link={type:'relation',from:'甲',to:'星图',relationType:'创作'};
afterEach(()=>{cleanup();vi.restoreAllMocks();});
it('keeps observed topics separate from active preferences and links both to evidence',()=>{
  const ref='qq:group:1:23';
  const g=memberGraph({messages:5,segments:[],members:[{id:'7',name:'青',aliases:[],count:5,first:'',last:'',keywords:[],
    stored_profile:{key:'snapshot',text_count:5,active_days:2,median_chars:3,traits:[{kind:'topics',label:'星图',count:3,evidence_ids:[ref]}]},
    claims:[{key:'pref',summary:'我喜欢跑团',evidence_id:ref,status:'active',current_preference:true},
      {key:'old',summary:'旧说法',evidence_id:ref,status:'revoked',current_preference:false}]}]});
  expect(g.links.map(l=>l.label)).toEqual(expect.arrayContaining(['讨论话题','自述喜好','依据']));
  expect(g.nodes.filter(n=>n.category==='evidence')).toHaveLength(1);
  expect(g.nodes.some(n=>n.label==='旧说法')).toBe(false);
});
it('preserves observations and relation direction across MCP formats',()=>{
  const expected=parseMcp(JSON.stringify({entities:[a,b],relations:[link]}));
  expect(parseMcp([link,a,b].map(x=>JSON.stringify(x)).join('\r\n'))).toEqual(expected);
  expect(parseMcp(JSON.stringify([a,b,link]))).toEqual(expected);
  expect(filterGraph(expected,'整理','人物','','').nodes.map(n=>n.label)).toEqual(['甲']);
  expect(filterGraph(expected,'','','创作','').links[0]).toEqual({source:'甲',target:'星图',label:'创作'});
  expect(filterGraph(expected,'','作品','创作','').nodes).toEqual([]);
  expect(()=>parseMcp(JSON.stringify([a,link]))).toThrow('未定义实体');
});
it('supports pasted text and dropped files',async()=>{
  const loaded=vi.fn();
  render(<GraphImport onLoad={loaded}/>);
  const raw=JSON.stringify([a,b,link]);
  fireEvent.change(screen.getByLabelText('MCP 图谱文本'),{target:{value:raw}});
  fireEvent.click(screen.getByText('载入粘贴内容'));
  expect(loaded).toHaveBeenCalledTimes(1);
  fireEvent.drop(screen.getByLabelText('MCP 图谱文本'),{dataTransfer:{files:[{size:raw.length,text:async()=>raw}]}});
  await waitFor(()=>expect(loaded).toHaveBeenCalledTimes(2));
});
const segment={id:'s1',count:6,start:'2026-11-01T08:00:00Z',end:'2026-11-01T08:01:00Z',members:2,keywords:['星图','观测'],example_id:'m'};
it('counts shared segments once and opens matched messages or full context',async()=>{
  const segments=[segment,{...segment,id:'s2',keywords:['观测','星图','星图']}];
  const graph=keywordGraph({segments,members:[],messages:12});
  expect(graph.links).toHaveLength(1);
  expect(graph.links[0].count).toBe(2);
  const speech=vi.spyOn(studioApi,'groupSpeech').mockResolvedValue({items:[{message_id:'m',text:'我画了星图',sender_name:'甲',sender_id:'11',sent_at:segment.start}],more:false});
  render(<KeywordDetail word="星图" segments={segments} gid="123"/>);
  expect(await screen.findByText('我画了星图')).toBeTruthy();
  expect(speech.mock.lastCall?.[0]).toMatchObject({word:'星图',segment_id:'s1',group_id:'123'});
  fireEvent.change(screen.getByRole('combobox'),{target:{value:'s2'}});
  fireEvent.click(screen.getByLabelText('展开整段上下文'));
  await waitFor(()=>expect(speech.mock.lastCall?.[0]).toMatchObject({word:'',segment_id:'s2'}));
});
