# History cache for printer jobs
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import time
import logging
from asyncio import Lock
from ..common import JobEvent, RequestType

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    Optional,
    Dict,
    List,
    Generic,
    TypeVar
)

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .database import MoonrakerDatabase as DBComp
    from .job_state import JobState
    from .file_manager.file_manager import FileManager
    Totals = Dict[str, Union[float, int]]

_T = TypeVar("_T")
HIST_NAMESPACE = "history"
MAX_JOBS = 10000
BASE_TOTALS = {
    "total_jobs": 0,
    "total_time": 0.,
    "total_print_time": 0.,
    "total_filament_used": 0.,
    "longest_job": 0.,
    "longest_print": 0.
}

class History:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.file_manager: FileManager = self.server.lookup_component(
            'file_manager')
        self.request_lock = Lock()
        self.aux_fields: Dict[str, HistoryField] = {}
        database: DBComp = self.server.lookup_component("database")
        self.job_totals: Totals = database.get_item(
            "moonraker", "history.job_totals", dict(BASE_TOTALS)
        ).result()
        self.aux_totals: Totals = database.get_item(
            "moonraker", "history.aux_totals", {}
        ).result()

        self.server.register_event_handler(
            "server:klippy_disconnect", self._handle_disconnect)
        self.server.register_event_handler(
            "server:klippy_shutdown", self._handle_shutdown)
        self.server.register_event_handler(
            "job_state:state_changed", self._on_job_state_changed)
        self.server.register_notification("history:history_changed")

        self.server.register_endpoint(
            "/server/history/job", RequestType.GET | RequestType.DELETE,
            self._handle_job_request
        )
        self.server.register_endpoint(
            "/server/history/list", RequestType.GET, self._handle_jobs_list
        )
        self.server.register_endpoint(
            "/server/history/totals", RequestType.GET, self._handle_job_totals
        )
        self.server.register_endpoint(
            "/server/history/reset_totals", RequestType.POST,
            self._handle_job_total_reset
        )
        self.server.register_endpoint(
            "/server/history/info", RequestType.GET, self._handle_info_request
        )

        database.register_local_namespace(HIST_NAMESPACE)
        self.history_ns = database.wrap_namespace(HIST_NAMESPACE,
                                                  parse_keys=False)

        self.current_job: Optional[PrinterJob] = None
        self.current_job_id: Optional[str] = None
        self.next_job_id: int = 0
        self.cached_job_ids = self.history_ns.keys().result()
        if self.cached_job_ids:
            self.next_job_id = int(self.cached_job_ids[-1], 16) + 1

    async def _handle_job_request(self,
                                  web_request: WebRequest
                                  ) -> Dict[str, Any]:
        async with self.request_lock:
            req_type = web_request.get_request_type()
            if req_type == RequestType.GET:
                job_id = web_request.get_str("uid")
                if job_id not in self.cached_job_ids:
                    raise self.server.error(f"Invalid job uid: {job_id}", 404)
                job = await self.history_ns[job_id]
                return {"job": self._prep_requested_job(job, job_id)}
            if req_type == RequestType.DELETE:
                all = web_request.get_boolean("all", False)
                if all:
                    deljobs = self.cached_job_ids
                    self.history_ns.clear()
                    self.cached_job_ids = []
                    self.next_job_id = 0
                    return {'deleted_jobs': deljobs}

                job_id = web_request.get_str("uid")
                if job_id not in self.cached_job_ids:
                    raise self.server.error(f"Invalid job uid: {job_id}", 404)

                self.delete_job(job_id)
                return {'deleted_jobs': [job_id]}
            raise self.server.error("Invalid Request Method")

    async def _handle_jobs_list(self,
                                web_request: WebRequest
                                ) -> Dict[str, Any]:
        async with self.request_lock:
            i = 0
            count = 0
            end_num = len(self.cached_job_ids)
            jobs: List[Dict[str, Any]] = []
            start_num = 0

            before = web_request.get_float("before", -1)
            since = web_request.get_float("since", -1)
            limit = web_request.get_int("limit", 50)
            start = web_request.get_int("start", 0)
            order = web_request.get_str("order", "desc")

            if order not in ["asc", "desc"]:
                raise self.server.error(f"Invalid `order` value: {order}", 400)

            reverse_order = (order == "desc")

            # cached jobs is asc order, find lower and upper boundary
            if since != -1:
                while start_num < end_num:
                    job_id = self.cached_job_ids[start_num]
                    job: Dict[str, Any] = await self.history_ns[job_id]
                    if job['start_time'] > since:
                        break
                    start_num += 1

            if before != -1:
                while end_num > 0:
                    job_id = self.cached_job_ids[end_num-1]
                    job = await self.history_ns[job_id]
                    if job['end_time'] < before:
                        break
                    end_num -= 1

            if start_num >= end_num or end_num == 0:
                return {"count": 0, "jobs": []}

            i = start
            count = end_num - start_num

            if limit == 0:
                limit = MAX_JOBS

            while i < count and len(jobs) < limit:
                if reverse_order:
                    job_id = self.cached_job_ids[end_num - i - 1]
                else:
                    job_id = self.cached_job_ids[start_num + i]
                job = await self.history_ns[job_id]
                jobs.append(self._prep_requested_job(job, job_id))
                i += 1

            return {"count": count, "jobs": jobs}

    async def _handle_job_totals(self, web_request: WebRequest) -> Dict[str, Totals]:
        return {
            "job_totals": self.job_totals,
            "auxiliary_totals": self.aux_totals
        }

    async def _handle_job_total_reset(
        self, web_request: WebRequest
    ) -> Dict[str, Totals]:
        if self.current_job is not None:
            raise self.server.error("Job in progress, cannot reset totals")
        last_totals = self.job_totals
        self.job_totals = dict(BASE_TOTALS)
        last_aux_totals = self.aux_totals
        self.aux_totals = {}
        for aux_field in self.aux_fields.values():
            if aux_field.available_totals is not None:
                self.aux_totals.update({key: 0 for key in aux_field.available_totals})
        database: DBComp = self.server.lookup_component("database")
        await database.insert_item("moonraker", "history.job_totals", self.job_totals)
        await database.insert_item("moonraker", "history.job_totals", self.aux_totals)
        return {
            "last_totals": last_totals,
            "last_auxiliary_totals": last_aux_totals
        }

    async def _handle_info_request(self, web_request: WebRequest) -> Dict[str, Any]:
        aux_fields = [field.get_field_info() for field in self.aux_fields.values()]
        return {
            "auxiliary_fields": aux_fields
        }

    def _on_job_state_changed(
        self,
        job_event: JobEvent,
        prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any]
    ) -> None:
        if job_event == JobEvent.STARTED:
            if self.current_job is not None:
                # Finish with the previous state
                self.finish_job("cancelled", prev_stats)
            self.add_job(PrinterJob(new_stats))
        elif job_event == JobEvent.COMPLETE:
            self.finish_job("completed", new_stats)
        elif job_event == JobEvent.ERROR:
            self.finish_job("error", new_stats)
        elif job_event in (JobEvent.CANCELLED, JobEvent.STANDBY):
            # Cancel on "standby" for backward compatibility with
            # `CLEAR_PAUSE/SDCARD_RESET_FILE` workflow
            self.finish_job("cancelled", prev_stats)

    def _handle_shutdown(self) -> None:
        jstate: JobState = self.server.lookup_component("job_state")
        last_ps = jstate.get_last_stats()
        self.finish_job("klippy_shutdown", last_ps)

    def _handle_disconnect(self) -> None:
        jstate: JobState = self.server.lookup_component("job_state")
        last_ps = jstate.get_last_stats()
        self.finish_job("klippy_disconnect", last_ps)

    def add_job(self, job: PrinterJob) -> None:
        if len(self.cached_job_ids) >= MAX_JOBS:
            self.delete_job(self.cached_job_ids[0])
        job_id = f"{self.next_job_id:06X}"
        self.current_job = job
        self.current_job_id = job_id
        self.grab_job_metadata()
        for aux_field in self.aux_fields.values():
            value = aux_field.on_job_start()
            job.set_auxiliary_data(aux_field.name, value)
        self.history_ns[job_id] = job.get_stats()
        self.cached_job_ids.append(job_id)
        self.next_job_id += 1
        logging.debug(
            f"History Job Added - Id: {job_id}, File: {job.filename}"
        )
        self.send_history_event("added")

    def delete_job(self, job_id: Union[int, str]) -> None:
        if isinstance(job_id, int):
            job_id = f"{job_id:06X}"

        if job_id in self.cached_job_ids:
            del self.history_ns[job_id]
            self.cached_job_ids.remove(job_id)

    def finish_job(self, status: str, pstats: Dict[str, Any]) -> None:
        if self.current_job is None:
            return
        cj = self.current_job
        if (
            pstats.get('filename') != cj.get('filename') or
            pstats.get('total_duration', 0.) < cj.get('total_duration')
        ):
            # Print stats have been reset, do not update this job with them
            pstats = {}

        self.current_job.finish(status, pstats)
        # Regrab metadata incase metadata wasn't parsed yet due to file upload
        self.grab_job_metadata()
        for aux_field in self.aux_fields.values():
            value = aux_field.on_job_finished()
            self.current_job.set_auxiliary_data(aux_field.name, value)
        self.save_current_job()
        self._update_job_totals()
        logging.debug(
            f"History Job Finished - Id: {self.current_job_id}, "
            f"File: {self.current_job.filename}, "
            f"Status: {status}"
        )
        self.send_history_event("finished")
        self.current_job = None
        self.current_job_id = None

    async def get_job(self,
                      job_id: Union[int, str]
                      ) -> Optional[Dict[str, Any]]:
        if isinstance(job_id, int):
            job_id = f"{job_id:06X}"
        return await self.history_ns.get(job_id, None)

    def grab_job_metadata(self) -> None:
        if self.current_job is None:
            return
        filename: str = self.current_job.get("filename")
        mdst = self.file_manager.get_metadata_storage()
        metadata: Dict[str, Any] = mdst.get(filename, {})
        if metadata:
            # Add the start time and job id to the
            # persistent metadata storage
            metadata.update({
                'print_start_time': self.current_job.get('start_time'),
                'job_id': self.current_job_id
            })
            mdst.insert(filename, metadata.copy())
        # We don't need to store these fields in the
        # job metadata, as they are redundant
        metadata.pop('print_start_time', None)
        metadata.pop('job_id', None)
        if "thumbnails" in metadata:
            thumb: Dict[str, Any]
            for thumb in metadata['thumbnails']:
                thumb.pop('data', None)
        self.current_job.set("metadata", metadata)

    def save_current_job(self) -> None:
        if self.current_job is None or self.current_job_id is None:
            return
        self.history_ns[self.current_job_id] = self.current_job.get_stats()

    def _update_job_totals(self) -> None:
        if self.current_job is None:
            return
        job = self.current_job
        self._accumulate_total("total_jobs", 1)
        self._accumulate_total("total_time", job.total_duration)
        self._accumulate_total("total_print_time", job.print_duration)
        self._accumulate_total("total_filament_used", job.filament_used)
        self._maximize_total("longest_job", job.total_duration)
        self._maximize_total("longest_print", job.print_duration)
        self._update_aux_totals()
        database: DBComp = self.server.lookup_component("database")
        database.insert_item("moonraker", "history.job_totals", self.job_totals)
        database.insert_item("moonraker", "history.aux_totals", self.aux_totals)

    def _accumulate_total(self, field: str, val: Union[int, float]) -> None:
        self.job_totals[field] += val

    def _maximize_total(self, field: str, val: Union[int, float]) -> None:
        self.job_totals[field] = max(self.job_totals[field], val)

    def _update_aux_totals(self) -> None:
        for aux_field in self.aux_fields.values():
            if aux_field.available_totals is None:
                continue
            for field in aux_field.available_totals:
                last_val = self.aux_totals.get(field, 0)
                self.aux_totals[field] = aux_field.get_totals(field, last_val)

    def send_history_event(self, evt_action: str) -> None:
        if self.current_job is None or self.current_job_id is None:
            return
        job = self._prep_requested_job(
            self.current_job.get_stats(), self.current_job_id)
        self.server.send_event("history:history_changed",
                               {'action': evt_action, 'job': job})

    def _prep_requested_job(self,
                            job: Dict[str, Any],
                            job_id: str
                            ) -> Dict[str, Any]:
        mtime = job.get("metadata", {}).get("modified", None)
        job['job_id'] = job_id
        job['exists'] = self.file_manager.check_file_exists(
            "gcodes", job['filename'], mtime
        )
        return job

    def register_auxiliary_field(
        self, aux_field: HistoryField
    ) -> None:
        if aux_field.name in self.aux_fields:
            raise self.server.error(
                f"Field named '{aux_field.name}' already registered."
            )
        logging.debug(f"Registering auxiliary field: {aux_field.name}")
        new_aux_totals = aux_field.available_totals
        if new_aux_totals is not None:
            registered_aux_totals = []
            for hist_field in self.aux_fields.values():
                if hist_field.available_totals is not None:
                    registered_aux_totals.extend(hist_field.available_totals)
            for new_aux_totals_field in new_aux_totals:
                if new_aux_totals_field in registered_aux_totals:
                    raise self.server.error(
                        f"Auxiliary totals name '{new_aux_totals_field}' "
                        "already registered."
                    )
                if new_aux_totals_field not in self.aux_totals:
                    self.aux_totals[new_aux_totals_field] = 0
        self.aux_fields[aux_field.name] = aux_field

    def on_exit(self) -> None:
        jstate: JobState = self.server.lookup_component("job_state")
        last_ps = jstate.get_last_stats()
        self.finish_job("server_exit", last_ps)

class PrinterJob:
    def __init__(self, data: Dict[str, Any] = {}) -> None:
        self.end_time: Optional[float] = None
        self.filament_used: float = 0
        self.filename: Optional[str] = None
        self.metadata: Optional[Dict[str, Any]] = None
        self.print_duration: float = 0.
        self.status: str = "in_progress"
        self.start_time = time.time()
        self.total_duration: float = 0.
        self.auxiliary_data: Dict[str, Any] = {}
        self.update_from_ps(data)

    def finish(self,
               status: str,
               print_stats: Dict[str, Any] = {}
               ) -> None:
        self.end_time = time.time()
        self.status = status
        self.update_from_ps(print_stats)

    def get(self, name: str) -> Any:
        if not hasattr(self, name):
            return None
        return getattr(self, name)

    def get_stats(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    def set(self, name: str, val: Any) -> None:
        if not hasattr(self, name):
            return
        setattr(self, name, val)

    def set_auxiliary_data(self, field_name: str, val: Any) -> None:
        self.auxiliary_data[field_name] = val

    def update_from_ps(self, data: Dict[str, Any]) -> None:
        for i in data:
            if hasattr(self, i):
                setattr(self, i, data[i])

class HistoryField(Generic[_T]):
    def __init__(
        self,
        field_name: str,
        units: Optional[str],
        desc: str,
        available_totals: Optional[List[str]] = None
    ) -> None:
        self._name = field_name
        self.units = units
        self.description = desc
        self._available_totals = available_totals

    @property
    def name(self) -> str:
        return self._name

    @property
    def available_totals(self) -> Optional[List[str]]:
        return self._available_totals

    def get_field_info(self) -> Dict[str, Any]:
        return {
            "name": self._name,
            "units": self.units,
            "description": self.description,
            "available_totals": self._available_totals
        }

    def on_job_start(self) -> _T:
        raise NotImplementedError

    def on_job_finished(self) -> _T:
        raise NotImplementedError

    def get_totals(
        self, field_name: str, last_total: Union[float, int]
    ) -> Union[float, int]:
        raise NotImplementedError

def load_component(config: ConfigHelper) -> History:
    return History(config)
