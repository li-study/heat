#!/bin/bash

#SBATCH --job-name=removeEdges
#SBATCH --output=removeEdges_%A_%a.out
#SBATCH --error=removeEdges_%A_%a.err
#SBATCH --array=0-149
#SBATCH --time=05:00:00
#SBATCH --ntasks=1
#SBATCH --mem=20G

datasets=(cora_ml citeseer ppi pubmed mit)
seeds=({0..29})

num_datasets=${#datasets[@]}
num_seeds=${#seeds[@]}

dataset_id=$((SLURM_ARRAY_TASK_ID / num_seeds % num_datasets ))
seed_id=$((SLURM_ARRAY_TASK_ID % (num_seeds) ))

dataset=${datasets[$dataset_id]}
seed=${seeds[$seed_id]}

edgelist=datasets/${dataset}/edgelist.tsv.gz 
output=edgelists/${dataset}

edgelist_f=$(printf "${output}/seed=%03d/training_edges/edgelist.tsv" ${seed} )

if [ ! -f $edgelist_f  ]
then
	module purge
	module load bluebear
	module load future/0.16.0-foss-2018b-Python-3.6.6

	args=$(echo --edgelist ${edgelist} \
	--output ${output} --seed $seed)

	python remove_edges.py ${args}
fi