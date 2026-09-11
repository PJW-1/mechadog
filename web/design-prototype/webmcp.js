import {ROBOTS} from './operations.js';

// Optional imperative page tools. They share UI state and cannot contact a robot.
export function registerPageTools({document,store,navigate,onError=()=>{}}){
 const registry=document.modelContext;if(!registry?.registerTool)return ()=>{};
 const lifetime=new document.defaultView.AbortController();
 const inputObject=input=>{if(!input||typeof input!=='object'||Array.isArray(input))throw new Error('Expected an input object.');return input};
 const tools=[
  {name:'read_operations_summary',title:'관제 상태 읽기',description:'Read browser preview state and visible event counts. Real telemetry is not connected; imported files are recorded evidence, not live data.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute(input){if(Object.keys(inputObject(input)).length)throw new Error('No arguments expected.');return {liveConnected:false,source:store.demo?'browser-preview':'awaiting-real-data',selectedRobot:store.selected,mission:{...store.mission},previewStop:store.estop,manualCommand:store.command,events:store.queryEvents().map(event=>({id:event.id,source:event.source,title:event.title,review:event.review}))}}},
  {name:'open_operations_panel',title:'관제 작업 화면 열기',description:'Open a visible operational panel. Navigation releases browser-only manual control; it creates no mission and sends no device commands.',inputSchema:{type:'object',properties:{page:{type:'string',enum:['dashboard','missions','events','records','zones','devices','settings']}},required:['page'],additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:false},execute(input){const value=inputObject(input);if(Object.keys(value).length!==1||!['dashboard','missions','events','records','zones','devices','settings'].includes(value.page))throw new Error('Unknown operations page.');navigate(value.page);return {page:value.page,liveConnected:false}}},
  {name:'select_observation_robot',title:'관측 로봇 선택',description:'Select a browser-preview camera robot, releasing browser-only manual control. This does not assign, move or connect any real robot.',inputSchema:{type:'object',properties:{robot:{type:'string',enum:ROBOTS}},required:['robot'],additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:false},execute(input){const value=inputObject(input);if(Object.keys(value).length!==1||!ROBOTS.includes(value.robot))throw new Error('Unknown preview robot.');store.selectRobot(value.robot);return {selectedRobot:store.selected,source:'browser-preview',liveConnected:false}}}
 ];
 for(const tool of tools){try{Promise.resolve(registry.registerTool(tool,{signal:lifetime.signal})).catch(onError)}catch(error){onError(error)}}
 return ()=>lifetime.abort();
}
