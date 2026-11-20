// @vitest-environment jsdom
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import MemoryGraph3D from '../MemoryGraph3D';

const mockScene=vi.hoisted(()=>({onAfterRender:()=>{}}));
vi.mock('3d-force-graph',()=>({default:class {
  backgroundColor(){return this;} height(){return this;} width(){return this;}
  nodeColor(){return this;} nodeVal(){return this;} nodeLabel(){return this;}
  linkColor(){return this;} linkOpacity(){return this;} linkDirectionalArrowLength(){return this;}
  linkLabel(){return this;} graphData(){return this;} onNodeClick(){return this;}
  scene(){return mockScene;}
  renderer(){return {info:{render:{calls:4,triangles:230}}};}
  _destructor(){}
}}));
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.useRealTimers();});
it('measures rendered scene frames and detaches the monitor when closed',()=>{
  vi.useFakeTimers({toFake:['setInterval','clearInterval','performance']});
  vi.stubGlobal('ResizeObserver',class {observe(){} disconnect(){}});
  const original=mockScene.onAfterRender;
  render(<MemoryGraph3D nodes={[]} links={[]} onSelect={()=>{}}/>);
  fireEvent.click(screen.getByLabelText('3D 性能'));
  act(()=>{for(let i=0;i<30;i++) mockScene.onAfterRender();vi.advanceTimersByTime(1000);});
  expect(screen.getByText(/30 FPS/)).toBeTruthy();
  expect(screen.getByText(/230 个三角形/)).toBeTruthy();
  fireEvent.click(screen.getByLabelText('3D 性能'));
  expect(mockScene.onAfterRender).toBe(original);
});
