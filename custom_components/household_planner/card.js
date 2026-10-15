/* Household Planner — AGPL-3.0-or-later. No external assets or telemetry. */
class HouseholdPlannerCard extends HTMLElement {
  setConfig(config) {
    if (!config.config_entry_id) throw new Error('config_entry_id is required (Integration page URL).');
    this.config = config;
    if (!this.root) this.root = this.attachShadow({mode:'open'});
  }
  set hass(hass) { this._hass = hass; if (!this.data && !this.busy) this.refresh(); }
  connectedCallback() { this.timer = setInterval(()=>this.refresh(),10000); }
  disconnectedCallback() { clearInterval(this.timer); }
  getCardSize() { return 5; }
  async call(service, data={}) {
    const reply = await this._hass.callWS({type:'call_service',domain:'household_planner',service,
      service_data:{config_entry_id:this.config.config_entry_id,...data},return_response:true});
    return reply.response;
  }
  async refresh() {
    if (!this._hass || !this.config || this.busy || this.settings || this.preview) return;
    this.busy=true;
    try {this.data=await this.call('get_today');this.error='';} catch(e) {this.error=e.message||String(e);}
    finally {this.busy=false;this.render();}
  }
  async action(fn) {
    if(this.busy)return;this.busy=true;
    try {await fn();this.error='';} catch(e) {this.error=e.message||String(e);}
    finally {this.busy=false;this.render();}
  }
  node(tag,text,parent) {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;parent.append(n);return n;}
  button(label,parent,fn) {const b=this.node('button',label,parent);b.disabled=!!this.busy;b.onclick=()=>this.action(fn);return b;}
  render() {
    if(!this.root)return;
    this.root.innerHTML='<style>:host{display:block}ha-card{padding:18px}h2{margin-top:0}button,input,select{font:inherit;padding:8px;margin:5px 5px 5px 0}button{cursor:pointer}li{margin:12px 0}small{display:block;opacity:.8}.error{color:var(--error-color,#b00)}.task{border-top:1px solid #8884;padding:10px 0}</style>';
    const c=this.node('ha-card',undefined,this.root);this.node('h2',this.config.title||'Household Planner',c);
    if(this.error)this.node('p',this.error,c).className='error';
    if(!this.data)return;
    const d=this.data, caps=d.capabilities||{},s=d.summary||{};
    this.node('p',`${s.open_tasks??0} open · ${s.completed_points??0}/${s.planned_points??0} points completed · budget ${s.budget_today??'–'}`,c);
    this.node('small',`${d.date||''} · ${d.planner_state||''}${d.last_error_code?' · '+d.last_error_code:''}`,c);
    const list=this.node('ul',undefined,c);
    for(const t of d.tasks||[]) {
      const li=this.node('li',`${t.title||t.task_id} · ${t.weight} points · ${t.status||''}`,list);
      if(caps.complete && t.status!=='completed')this.button('Done',li,async()=>{
        const request_id=crypto.randomUUID();
        await this.call('complete_task',{plan_id:d.plan_id,plan_revision:d.plan_revision,occurrence_id:t.occurrence_id,request_id});
        this.pending=request_id;this.data=await this.call('get_today');
      });
    }
    if(this.pending && caps.cancel_pending)this.button('Undo queued completion (5 seconds)',c,async()=>{
      await this.call('cancel_pending',{request_id:this.pending});this.pending=null;this.data=await this.call('get_today');
    });
    for(const conflict of d.conflicts||[])this.node('small',typeof conflict==='string'?conflict:JSON.stringify(conflict),c);
    if(caps.sync)this.button('Sync',c,async()=>{await this.call('sync');this.data=await this.call('get_today');});
    if(caps.generate&&!d.plan_id)this.button('Create today’s plan',c,async()=>{await this.call('generate_today_plan');this.data=await this.call('get_today');});
    if(caps.replan&&d.plan_id)this.button('Preview new plan',c,async()=>{this.preview=await this.call('replan_today',{plan_id:d.plan_id,plan_revision:d.plan_revision,dry_run:true});});
    if(this.preview){
      this.node('p',`Preview: ${this.preview.planned_points??'?'} points. Existing completions are preserved.`,c);
      this.button('Apply preview',c,async()=>{await this.call('replan_today',{plan_id:d.plan_id,plan_revision:d.plan_revision,dry_run:false,preview_id:this.preview.preview_id,request_id:crypto.randomUUID()});this.preview=null;this.data=await this.call('get_today');});
      this.button('Cancel preview',c,async()=>{this.preview=null;});
    }
    if(caps.manage)this.button(this.settings?'Close settings':'Settings',c,async()=>{this.settings=this.settings?null:await this.call('get_settings');});
    if(this.settings){
      this.node('h3','Daily budgets (Monday → Sunday)',c);
      const budgets=this.node('input',undefined,c);budgets.value=(this.settings.budgets||d.week_budgets||[]).join(',');
      this.button('Save budgets',c,async()=>{await this.call('set_week_budget',{budgets:budgets.value.split(',').map(x=>Number(x.trim()))});this.settings=await this.call('get_settings');});
      this.node('p','Set effort, room and vacuum flag for each task before planning. Create or change recurrence in Donetick.',c);
      for(const task of this.settings.tasks||[]){
        const row=this.node('div',undefined,c);row.className='task';this.node('strong',task.title||task.task_id,row);
        const meta=task.metadata||{};
        const weight=this.node('select',undefined,row);for(const v of [1,2,3]){const o=this.node('option',String(v),weight);o.value=v;}weight.value=meta.weight||1;
        const area=this.node('input',undefined,row);area.placeholder='Room';area.value=meta.area||'';
        const label=this.node('label','Vacuum ',row),vac=this.node('input',undefined,label);vac.type='checkbox';vac.checked=!!meta.contains_vacuuming;
        this.button('Save task',row,async()=>{await this.call('set_task_metadata',{task_id:String(task.task_id),weight:Number(weight.value),area:area.value,contains_vacuuming:vac.checked,fixed_date:meta.fixed_date||null,advance_days:meta.advance_days||0});this.settings=await this.call('get_settings');});
      }
    }
  }
}
customElements.define('household-planner-card',HouseholdPlannerCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'household-planner-card',name:'Household Planner',description:'Daily chores, completion and planner settings.'});
