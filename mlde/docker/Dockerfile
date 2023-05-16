FROM nvcr.io/nvidia/nemo:23.01


COPY dockerfile_scripts /tmp/det_dockerfile_scripts
RUN /tmp/det_dockerfile_scripts/install_libnss_determined.sh
RUN /tmp/det_dockerfile_scripts/add_det_nobody_user.sh
RUN python -m pip install determined numpy==1.22.2 protobuf==3.20.1 && python -m pip uninstall -y determined
RUN python -m pip install lightning

RUN rm -r /tmp/*

RUN pip uninstall -y nemo-toolkit

RUN git clone https://github.com/NVIDIA/NeMo
RUN cd NeMo && pip install -r requirements/requirements_nlp.txt
RUN rm -r NeMo
