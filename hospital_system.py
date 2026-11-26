from collections import deque
from typing import Dict, List, Optional, Tuple


DEFAULT_DOCTORS: List[Dict[str, str]] = [
    {"name": "Dr. Aibtasam Ali", "field": "Pediatrics", "status": "Available"},
    {"name": "Dr. Ali Hamza", "field": "General Medicine", "status": "Available"},
    {"name": "Dr. Hifza", "field": "ENT & Dermatology", "status": "Available"},
    {"name": "Dr. Shawal", "field": "Cardiology", "status": "Available"},
    {"name": "Dr. Abubakar", "field": "Orthopedics", "status": "On Duty"},
    {"name": "Dr. Usman", "field": "Neurology", "status": "Available"},
]


class PatientNode:
    """SINGLY LINKED LIST NODE — HOLDS ONE PATIENT'S DATA"""

    def __init__(
        self,
        patient_id: int,
        name: str,
        age: int,
        disease: str,
        doctor: str = "General OPD",
        status: str = "Registered",
    ) -> None:
        self.id: int = patient_id
        self.name: str = name
        self.age: int = age
        self.disease: str = disease
        self.doctor: str = doctor
        self.status: str = status
        self.next: Optional["PatientNode"] = None

    def __repr__(self) -> str:
        return (
            f"PatientNode(id={self.id}, name={self.name!r}, "
            f"age={self.age}, disease={self.disease!r}, "
            f"doctor={self.doctor!r}, status={self.status!r})"
        )


class HospitalSystem:
    """
 MAIN CLASS STARTS FROM HERRRREEEEEEEEEEEEEEEEEE :)
    """

    def __init__(self) -> None:
        self.head: Optional[PatientNode] = None
        self.appointments: deque[Dict[str, str]] = deque()
        self.doctors: List[Dict[str, str]] = [dict(d) for d in DEFAULT_DOCTORS]
        self.current_attending: Optional[Dict[str, str]] = None
        self._patient_count: int = 0

    @property
    def patient_count(self) -> int:
        """WANT TO KNOOOOOW TOTAL PETIENTS??? USE THIS FUN())))))))"""
        return self._patient_count

    @property
    def pending_appointments_count(self) -> int:
        """HOW MANY PEOPLE ARE STILL WAITING IN LINE??"""
        return len(self.appointments)

    @property
    def available_doctors_count(self) -> int:
        """CHECK WHICH DOCTORS ARE ACTUALLY FREE RIGHT NOW"""
        return sum(1 for d in self.doctors if d["status"] == "Available")

    @property
    def next_in_queue(self) -> Optional[str]:
        """PEEKING AT THE NEXT PATIENT (NO POPPING YET, O(1) FLEX)"""
        return self.appointments[0]["name"] if self.appointments else None

    @property
    def next_appointment(self) -> Optional[Dict[str, str]]:
        """SAME PEEK BUT WITH DOCTOR NAME TOO :)"""
        return dict(self.appointments[0]) if self.appointments else None

    def _find_patient_by_name(self, name: str) -> Optional[PatientNode]:
        """Syncinngggggggggggggg patients."""
        target = name.strip().lower()
        curr = self.head
        while curr is not None:
            if curr.name.lower() == target:
                return curr
            curr = curr.next
        return None

    def add_patient(
        self,
        patient_id: int,
        name: str,
        age: int,
        disease: str,
        doctor: str = "General OPD",
    ) -> Tuple[bool, str]:
        """
        adding new petient in instance.....
        """
        if not isinstance(patient_id, int) or isinstance(patient_id, bool) or patient_id <= 0:
            return False, "Patient ID must be a positive integer."

        clean_name = name.strip() if isinstance(name, str) else ""
        if not clean_name:
            return False, "Patient name cannot be empty."

        if not isinstance(age, int) or isinstance(age, bool) or not (0 <= age <= 150):
            return False, "Patient age must be an integer between 0 and 150."

        clean_disease = disease.strip() if isinstance(disease, str) else ""
        if not clean_disease:
            return False, "Disease / medical condition cannot be empty."

        clean_doctor = doctor.strip() if isinstance(doctor, str) and doctor.strip() else "General OPD"

        new_node = PatientNode(
            patient_id=patient_id,
            name=clean_name,
            age=age,
            disease=clean_disease,
            doctor=clean_doctor,
            status="Registered",
        )

        if self.head is None:
            self.head = new_node
            self._patient_count += 1
            return True, "Patient added successfully"

        curr = self.head
        while True:
            if curr.id == patient_id:
                return False, f"Patient with ID {patient_id} already exists."
            if curr.next is None:
                break
            curr = curr.next

        curr.next = new_node
        self._patient_count += 1
        return True, "Patient added successfully"

    def search_patient(self, patient_id: int) -> Optional[PatientNode]:
        """HUNTING DOWN A PATIENT BY ID IN THE LINKED LIST..."""
        if not isinstance(patient_id, int) or isinstance(patient_id, bool):
            return None

        curr = self.head
        while curr is not None:
            if curr.id == patient_id:
                return curr
            curr = curr.next
        return None

    def book_appointment(self, name: str, doctor: Optional[str] = None) -> Tuple[bool, str]:
        """THROWING PATIENT INTO THE FIFO QUEUE (O(1) SPEEDRUN)"""

        clean_name = name.strip() if isinstance(name, str) else ""
        if not clean_name:
            return False, "Patient name cannot be empty."

        matched_patient = self._find_patient_by_name(clean_name)
        if doctor and isinstance(doctor, str) and doctor.strip():
            assigned_doctor = doctor.strip()
        elif matched_patient is not None:
            assigned_doctor = matched_patient.doctor
        else:
            assigned_doctor = f"{self.doctors[0]['name']} — {self.doctors[0]['field']}"

        if matched_patient is not None:
            matched_patient.status = "In Queue"
            if doctor and doctor.strip():
                matched_patient.doctor = assigned_doctor

        self.appointments.append({"name": clean_name, "doctor": assigned_doctor})
        return True, f"Appointment booked for {clean_name}"

    def call_next(self) -> Tuple[bool, str]:
        """NEXTTTTT PATIENT UP!! POPPING FROM FRONT OF THE QUEUE"""
        if not self.appointments:
            return False, "No pending appointments"

        # if someone was already with the doc, mark them done first
        if self.current_attending is not None:
            prev_node = self._find_patient_by_name(self.current_attending["name"])
            if prev_node is not None and prev_node.status == "In Progress":
                prev_node.status = "Attended"

        entry = self.appointments.popleft()
        self.current_attending = entry

        matched_patient = self._find_patient_by_name(entry["name"])
        if matched_patient is not None:
            matched_patient.status = "In Progress"

        return True, f"Now attending: {entry['name']}"

    def display_patients(self) -> List[PatientNode]:
        """WALK THE WHOLE LINKED LIST AND DUMP ALL PATIENTS"""
        patients: List[PatientNode] = []
        curr = self.head
        while curr is not None:
            patients.append(curr)
            curr = curr.next
        return patients
